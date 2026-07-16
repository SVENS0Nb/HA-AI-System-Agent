from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from .redaction import redact_text


class ConfigAccessDenied(PermissionError):
    """Raised for a path outside the mounted configuration capability."""


class PermissiveLoader(yaml.SafeLoader):
    pass


def _construct_unknown(
    loader: PermissiveLoader, tag_suffix: str, node: yaml.Node
) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


PermissiveLoader.add_multi_constructor("!", _construct_unknown)


class ConfigReader:
    TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".txt", ".conf", ".log", ".jinja", ".j2"}
    SENSITIVE_PARTS = {
        "secrets.yaml",
        ".storage",
        "known_devices.yaml",
        ".cloud",
    }

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        allow_sensitive: bool,
        *,
        max_search_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.allow_sensitive = allow_sensitive
        self.max_search_bytes = max_search_bytes

    def _resolve(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ConfigAccessDenied(
                "Path must be relative to the Home Assistant config directory"
            )
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ConfigAccessDenied("Path escapes the Home Assistant config directory")
        normalized = candidate.relative_to(self.root).as_posix()
        if not self.allow_sensitive and any(
            normalized == part or normalized.startswith(f"{part}/")
            for part in self.SENSITIVE_PARTS
        ):
            raise ConfigAccessDenied(
                "Sensitive configuration is disabled in add-on options"
            )
        if (
            candidate.suffix.lower() not in self.TEXT_SUFFIXES
            and ".storage/" not in f"/{normalized}"
        ):
            raise ConfigAccessDenied(
                "Only text configuration and log files can be read"
            )
        return candidate

    def list_files(self, pattern: str = "*", limit: int = 200) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root).as_posix()
            if any(part in {"deps", "__pycache__", ".git"} for part in path.parts):
                continue
            if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(
                path.name, pattern
            ):
                continue
            try:
                self._resolve(relative)
            except ConfigAccessDenied:
                continue
            result.append({"path": relative, "bytes": path.stat().st_size})
            if len(result) >= max(1, min(limit, 500)):
                break
        return result

    def _read_raw(self, relative_path: str) -> str:
        path = self._resolve(relative_path)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(relative_path)
        size = path.stat().st_size
        if size > self.max_bytes:
            raise ValueError(
                f"File is {size} bytes; configured limit is {self.max_bytes} bytes"
            )
        return path.read_text(encoding="utf-8", errors="replace")

    def read(self, relative_path: str) -> str:
        return redact_text(self._read_raw(relative_path))

    def search(
        self, query: str, pattern: str = "*", limit: int = 50
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError("query must not be empty")
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        scanned_bytes = 0
        for item in self.list_files(pattern, 200):
            scanned_bytes += min(int(item["bytes"]), self.max_bytes)
            if scanned_bytes > self.max_search_bytes:
                break
            try:
                content = self.read(item["path"])
            except (OSError, ValueError, ConfigAccessDenied):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if needle in line.casefold():
                    matches.append(
                        {"path": item["path"], "line": number, "text": line[:500]}
                    )
                    if len(matches) >= max(1, min(limit, 200)):
                        return matches
        return matches

    def validate_yaml(self, relative_path: str) -> dict[str, Any]:
        content = self._read_raw(relative_path)
        try:
            list(yaml.load_all(content, Loader=PermissiveLoader))
            return {"valid_yaml_syntax": True, "path": relative_path}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            return {
                "valid_yaml_syntax": False,
                "path": relative_path,
                "error": str(exc),
                "line": mark.line + 1 if mark else None,
                "column": mark.column + 1 if mark else None,
            }

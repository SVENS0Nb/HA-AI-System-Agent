from __future__ import annotations

import re


class AdaptiveReasoningRouter:
    """Select reasoning effort locally without spending an extra API request."""

    EFFORTS = ("none", "low", "medium", "high")
    _SIMPLE = (
        "hallo",
        "hello",
        "danke",
        "thank",
        "status",
        "zustand",
        "wert",
        "value",
        "ist online",
        "is online",
        "sensor",
        "liste",
        "list ",
    )
    _DIAGNOSTIC = (
        "fehler",
        "error",
        "warnung",
        "warning",
        "log",
        "config",
        "konfiguration",
        "yaml",
        "diagnos",
        "analys",
        "warum",
        "why",
        "unavailable",
        "unknown",
        "ausfall",
        "ausgefallen",
        "nicht erreichbar",
    )
    _AUTOMATION = (
        "monitor",
        "überwach",
        "benachrichtig",
        "melde dich",
        "cron",
        "zeitplan",
        "schedule",
        "event",
        "trigger",
    )
    _CONTROL = (
        "schalte",
        "mach an",
        "mach aus",
        "öffne",
        "schließe",
        "entriegle",
        "verriegle",
        "stelle ",
        "turn on",
        "turn off",
        "switch on",
        "switch off",
        "unlock",
        "lock ",
        "open ",
        "close ",
        "set ",
    )
    _BREADTH = (
        "alle ",
        "all ",
        "mehrere",
        "multiple",
        "gesamte",
        "entire",
        "vergleich",
        "compare",
        "korrel",
        "zusammenhang",
        "über zeit",
        "over time",
    )
    _HIGH = (
        "ursachenanalyse",
        "root cause",
        "sporadisch",
        "intermittent",
        "flapping",
        "sicherheitsaudit",
        "security audit",
        "mehrere dateien",
        "multiple files",
        "komplex",
        "complex",
    )

    @classmethod
    def select(cls, task: str, *, proactive: bool = False) -> str:
        text = " ".join(task.casefold().split())
        if not text:
            return "medium" if proactive else "low"

        score = 1
        words = re.findall(r"[\w.-]+", text)
        has_diagnostic = any(marker in text for marker in cls._DIAGNOSTIC)
        has_automation = any(marker in text for marker in cls._AUTOMATION)
        has_control = any(marker in text for marker in cls._CONTROL)
        has_breadth = any(marker in text for marker in cls._BREADTH)

        if proactive or has_diagnostic:
            score = max(score, 2)
        if has_automation or has_control:
            score = max(score, 2)
        if (has_breadth and (has_diagnostic or has_automation)) or any(
            marker in text for marker in cls._HIGH
        ):
            score = 3
        if len(words) >= 80 or len(text) >= 600:
            score = 3
        elif len(words) >= 35 or len(text) >= 280:
            score = max(score, 2)

        has_config = any(
            marker in text for marker in ("config", "konfiguration", "yaml")
        )
        has_error = any(
            marker in text for marker in ("fehler", "error", "ungültig", "invalid")
        )
        if has_config and has_error:
            score = 3

        if (
            score == 1
            and len(words) <= 18
            and any(marker in text for marker in cls._SIMPLE)
        ):
            score = 0
        return cls.EFFORTS[score]

    @classmethod
    def escalate(cls, effort: str, minimum: str = "low") -> str:
        current = cls.EFFORTS.index(effort)
        floor = cls.EFFORTS.index(minimum)
        return cls.EFFORTS[min(max(current + 1, floor), len(cls.EFFORTS) - 1)]

    @classmethod
    def at_least(cls, effort: str, minimum: str) -> str:
        return cls.EFFORTS[max(cls.EFFORTS.index(effort), cls.EFFORTS.index(minimum))]

    @staticmethod
    def for_model(model: str, effort: str) -> str:
        """Map configured effort to the documented range of known GPT-5 models."""
        name = model.casefold().strip()
        if name.startswith("gpt-5.6-luna"):
            return effort
        if name.startswith("gpt-5-pro"):
            return "high"
        if name.startswith(("gpt-5.2-pro", "gpt-5.4-pro", "gpt-5.5-pro")):
            if effort in {"none", "low"}:
                return "medium"
            return "xhigh" if effort == "max" else effort
        if name.startswith(("gpt-5.2", "gpt-5.4", "gpt-5.5")):
            return "xhigh" if effort == "max" else effort
        # Unknown/custom model IDs pass through because aliases may expose a
        # newer range. The UI test performs a real Responses API request and
        # exposes any incompatibility before the runtime is started.
        return effort

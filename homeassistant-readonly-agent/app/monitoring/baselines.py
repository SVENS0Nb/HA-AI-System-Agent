from __future__ import annotations

from collections import OrderedDict
from typing import Protocol

from .models import BaselineModel, EntityFeature


class BaselineRepository(Protocol):
    def get_baseline(
        self, entity_id: str, context_key: str
    ) -> BaselineModel | None: ...

    def save_baseline(self, model: BaselineModel) -> None: ...

    def apply_baseline_updates(
        self, event_id: str, models: list[BaselineModel]
    ) -> bool: ...


class BaselineManager:
    """Conservative multi-level contextual baseline manager."""

    def __init__(
        self,
        repository: BaselineRepository,
        *,
        minimum_samples: int = 20,
        cache_size: int = 20_000,
    ) -> None:
        self.repository = repository
        self.minimum_samples = minimum_samples
        self.cache_size = cache_size
        self._cache: OrderedDict[tuple[str, str], BaselineModel] = OrderedDict()

    def select(self, feature: EntityFeature) -> BaselineModel | None:
        if feature.value is None:
            return None
        for key in self.context_keys(feature, specific_first=True):
            model = self._get(feature.entity_id, key)
            if model is not None and model.count >= self.minimum_samples:
                return model
        return None

    def global_model(self, entity_id: str) -> BaselineModel | None:
        return self._get(entity_id, "global")

    def update(
        self, feature: EntityFeature, *, event_id: str | None = None
    ) -> list[BaselineModel]:
        if feature.value is None:
            return []
        updated: list[BaselineModel] = []
        for key in self.context_keys(feature, specific_first=False):
            model = self._get(feature.entity_id, key) or BaselineModel(
                entity_id=feature.entity_id,
                context_key=key,
                created_at=feature.timestamp,
                updated_at=feature.timestamp,
            )
            model = model.updated(
                feature.value,
                feature.timestamp,
                feature.seconds_since_previous_update,
            )
            updated.append(model)
        if event_id is not None:
            if not self.repository.apply_baseline_updates(event_id, updated):
                refreshed: list[BaselineModel] = []
                for model in updated:
                    stored = self.repository.get_baseline(
                        model.entity_id, model.context_key
                    )
                    if stored is not None:
                        self._put(stored)
                        refreshed.append(stored)
                return refreshed
        else:
            for model in updated:
                self.repository.save_baseline(model)
        for model in updated:
            self._put(model)
        return updated

    @staticmethod
    def context_keys(
        feature: EntityFeature, *, specific_first: bool
    ) -> tuple[str, ...]:
        season = feature.context.get("season", "unknown")
        day_type = feature.context.get("day_type", "unknown")
        time_bucket = feature.context.get("time_bucket", "unknown")
        general = (
            "global",
            f"season={season}",
            f"day_type={day_type}",
            f"time_bucket={time_bucket}",
            f"season={season}|day_type={day_type}|time_bucket={time_bucket}",
        )
        return tuple(reversed(general)) if specific_first else general

    def _get(self, entity_id: str, context_key: str) -> BaselineModel | None:
        key = (entity_id, context_key)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        model = self.repository.get_baseline(entity_id, context_key)
        if model is not None:
            self._put(model)
        return model

    def _put(self, model: BaselineModel) -> None:
        key = (model.entity_id, model.context_key)
        self._cache[key] = model
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

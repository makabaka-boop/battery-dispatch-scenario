"""线程安全的内存情景存储 + 乐观并发（预期修订号）。"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from .models import ScenarioInput


class RevisionConflict(Exception):
    """保存时携带的 expected_revision 与当前修订号不一致。"""


class NotFound(Exception):
    """情景不存在。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScenarioStore:
    """内存存储；每次成功保存 revision 自增，用 RLock 保证并发原子性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, dict] = {}

    def create(self, scenario: ScenarioInput) -> dict:
        new_id = uuid.uuid4().hex[:12]
        ts = _now()
        row = {
            "id": new_id,
            "revision": 1,
            "scenario": scenario.model_copy(deep=True),
            "created_at": ts,
            "updated_at": ts,
        }
        with self._lock:
            self._rows[new_id] = row
            return self._snapshot(row)

    def get(self, scenario_id: str) -> dict:
        with self._lock:
            row = self._rows.get(scenario_id)
            if row is None:
                raise NotFound(scenario_id)
            return self._snapshot(row)

    def save(
        self,
        scenario_id: str,
        scenario: ScenarioInput,
        expected_revision: int,
    ) -> dict:
        """仅当 expected_revision == 当前 revision 时覆盖并自增，否则抛 RevisionConflict。"""
        with self._lock:
            row = self._rows.get(scenario_id)
            if row is None:
                raise NotFound(scenario_id)
            if expected_revision != row["revision"]:
                raise RevisionConflict(
                    f"修订号冲突：预期 {expected_revision}，当前 {row['revision']}"
                )
            row["scenario"] = scenario.model_copy(deep=True)
            row["revision"] += 1
            row["updated_at"] = _now()
            return self._snapshot(row)

    @staticmethod
    def _snapshot(row: dict) -> dict:
        return {
            "id": row["id"],
            "revision": row["revision"],
            "scenario": row["scenario"].model_copy(deep=True),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

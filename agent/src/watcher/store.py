"""Filesystem-backed persistence for market watchers.

Ported verbatim from the Vibe-Trading (v0.1.6) watcher subsystem. The store is
constructed with an explicit ``base_dir`` so it stays decoupled from the
runtime-root resolution; :func:`default_watchers_dir` supplies the conventional
location under this codebase's data directory.

Directory structure::

    watchers/
    └── {watcher_id}/
        ├── watcher.json
        └── events.jsonl
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.watcher.models import WatchItem, WatchStatus, WatcherEvent, utc_now_iso


def default_watchers_dir() -> Path:
    """Return the conventional watcher storage directory.

    Lives under the runtime data directory (``~/.vibe-trading`` by default),
    alongside ``channels/`` and ``pairing.json``.
    """
    from src.config.paths import get_data_dir

    return get_data_dir() / "watchers"


class WatcherStore:
    """Persistent watcher storage.

    Directory structure::

        watchers/
        └── {watcher_id}/
            ├── watcher.json
            └── events.jsonl
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    def watcher_dir(self, watcher_id: str) -> Path:
        return self.base_dir / watcher_id

    def watcher_file(self, watcher_id: str) -> Path:
        return self.watcher_dir(watcher_id) / "watcher.json"

    def events_file(self, watcher_id: str) -> Path:
        return self.watcher_dir(watcher_id) / "events.jsonl"

    def deleted_marker(self, watcher_id: str) -> Path:
        return self.base_dir / ".deleted" / watcher_id

    def _is_deleted(self, watcher_id: str) -> bool:
        return self.deleted_marker(watcher_id).exists()

    def create(self, item: WatchItem) -> WatchItem:
        item.pair = item.pair.strip().upper()
        item.asset_class = item.asset_class.strip().lower() or "crypto"
        item.direction = item.direction.strip().upper()
        item.touch()
        if not item.watcher_state.armed_at:
            item.watcher_state.armed_at = item.updated_at
        directory = self.watcher_dir(item.id)
        if directory.exists() or self._is_deleted(item.id):
            raise ValueError(f"Watcher {item.id} already exists")
        directory.mkdir(parents=True)
        self._write_json(self.watcher_file(item.id), item.to_dict())
        return item

    def get(self, watcher_id: str) -> Optional[WatchItem]:
        if self._is_deleted(watcher_id):
            shutil.rmtree(self.watcher_dir(watcher_id), ignore_errors=True)
            return None
        data = self._read_json(self.watcher_file(watcher_id))
        if data is None:
            return None
        try:
            item = WatchItem.from_dict(data)
        except (TypeError, ValueError):
            return None
        if not item.watcher_state.armed_at:
            item.watcher_state.armed_at = self._infer_armed_at(item)
        return item

    def update(self, item: WatchItem) -> None:
        if self._is_deleted(item.id) or not self.watcher_file(item.id).exists():
            return
        item.touch()
        self._write_json(self.watcher_file(item.id), item.to_dict())

    def delete(self, watcher_id: str) -> bool:
        directory = self.watcher_dir(watcher_id)
        existed = directory.exists()
        marker = self.deleted_marker(watcher_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(utc_now_iso(), encoding="utf-8")
        if not existed:
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    def list(self, *, include_terminal: bool = True, limit: int = 200) -> List[WatchItem]:
        items: list[WatchItem] = []
        if not self.base_dir.exists():
            return items
        for directory in self.base_dir.iterdir():
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if self._is_deleted(directory.name):
                shutil.rmtree(directory, ignore_errors=True)
                continue
            item = self.get(directory.name)
            if item is None:
                continue
            if not include_terminal and item.status != WatchStatus.ACTIVE:
                continue
            items.append(item)
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[:limit]

    def set_status(self, watcher_id: str, status: WatchStatus, *, reason: str = "") -> Optional[WatchItem]:
        item = self.get(watcher_id)
        if item is None:
            return None
        item.status = status
        item.watcher_state.last_event = status.value
        if status == WatchStatus.ACTIVE:
            item.watcher_state.armed_at = utc_now_iso()
        self.update(item)
        self.append_event(
            WatcherEvent(
                watcher_id=watcher_id,
                event_type=f"watcher.{status.value}",
                message=reason or f"Watcher marked {status.value}.",
            )
        )
        return item

    def append_event(self, event: WatcherEvent) -> None:
        if self._is_deleted(event.watcher_id) or not self.watcher_file(event.watcher_id).exists():
            return
        path = self.events_file(event.watcher_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._write_lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read_events(self, watcher_id: str, *, after_index: int = 0, limit: int = 200) -> List[WatcherEvent]:
        path = self.events_file(watcher_id)
        if not path.exists():
            return []
        events: list[WatcherEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines[after_index:]:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(WatcherEvent.from_dict(json.loads(stripped)))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if len(events) >= limit:
                break
        return events

    def _infer_armed_at(self, item: WatchItem) -> str:
        armed_at = item.created_at
        for event in self.read_events(item.id, limit=1000):
            if event.event_type in {"watcher.created", "watcher.updated", "watcher.active"}:
                armed_at = event.created_at
        return armed_at

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_json(self, path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with self._write_lock:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)

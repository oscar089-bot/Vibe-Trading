"""Deterministic market watchers.

A watcher is a machine-checkable market condition attached to a session. A
separate background loop evaluates it against live market data with no LLM in
the hot path; when the condition first becomes true it wakes the owning
session so the model can re-analyse and re-arm for the next phase.

Ported from the Vibe-Trading (v0.1.6) watcher subsystem, with the loop and
market-data layers rebuilt on this codebase's loader registry and asyncio
service idioms (see the OpenAlice watcher port for the design rationale).
"""

from src.watcher.models import (
    PriceZone,
    WatchItem,
    WatchKind,
    WatchStatus,
    WatcherEvent,
    WatcherEventType,
    WatcherState,
    utc_now_iso,
)

__all__ = [
    "PriceZone",
    "WatchItem",
    "WatchKind",
    "WatchStatus",
    "WatcherEvent",
    "WatcherEventType",
    "WatcherState",
    "utc_now_iso",
]

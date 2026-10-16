"""Data models for persistent deterministic market watchers.

Ported from the Vibe-Trading (v0.1.6) watcher subsystem. The runtime-state
fields ``spec_fingerprint``, ``backoff_until`` and ``terminal`` are carried
over from the OpenAlice port, where they proved necessary in production:

* ``spec_fingerprint`` lets the loop detect that the model rewrote the rule
  set (a re-arm) and reset fired-condition history, so the previous phase's
  firing cannot suppress the new phase's rules.
* ``backoff_until`` is a persisted deadline for a repeatedly failing market
  fetch, so a restart cannot reset the backoff and hammer a dead provider.
* ``terminal`` marks a watcher that reached a terminal state, so the loop
  stops spending fetches on a dead condition.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


class WatchStatus(str, Enum):
    """Watcher lifecycle states."""

    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class WatchKind(str, Enum):
    """Supported watcher kinds."""

    WAITING_SETUP = "waiting_setup"
    ACTIVE_TRADE = "active_trade"


class WatcherEventType(str, Enum):
    """Structured watcher event types."""

    TRIGGER_CLOSE = "TRIGGER_CLOSE"
    TRIGGER_HIT = "TRIGGER_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    MANAGEMENT_ALERT = "MANAGEMENT_ALERT"
    LLM_CONFIRMATION_NEEDED = "LLM_CONFIRMATION_NEEDED"
    PRICE_CHECK = "PRICE_CHECK"
    ERROR = "ERROR"


TERMINAL_STATUSES = {
    WatchStatus.TRIGGERED,
    WatchStatus.INVALIDATED,
    WatchStatus.EXPIRED,
    WatchStatus.CANCELLED,
    WatchStatus.DELETED,
}


@dataclass
class PriceZone:
    """A low/high price zone."""

    low: float
    high: float

    def normalized(self) -> "PriceZone":
        """Return the zone with ``low <= high``."""
        low = float(self.low)
        high = float(self.high)
        if low <= high:
            return PriceZone(low=low, high=high)
        return PriceZone(low=high, high=low)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self.normalized())

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["PriceZone"]:
        if not isinstance(data, dict):
            return None
        try:
            return cls(low=float(data["low"]), high=float(data["high"])).normalized()
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class WatcherState:
    """Mutable state used for polling, anti-spam, and restart recovery."""

    last_checked: Optional[str] = None
    armed_at: Optional[str] = None
    last_market_timestamp: Optional[str] = None
    last_price: Optional[float] = None
    last_candle: Optional[Dict[str, Any]] = None
    last_confirmed_candle: Optional[Dict[str, Any]] = None
    previous_confirmed_candle: Optional[Dict[str, Any]] = None
    last_event: Optional[str] = None
    escalated: bool = False
    llm_reconfirmed: bool = False
    alert_sent_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    poll_count: int = 0
    alerted_conditions: Dict[str, str] = field(default_factory=dict)
    fired_conditions: Dict[str, str] = field(default_factory=dict)
    error_count: int = 0
    last_error: Optional[str] = None
    # --- OpenAlice port additions ---
    #: Fingerprint of the authored spec this state belongs to. A change means
    #: the model rewrote its rule set (re-armed), so fired-condition history is
    #: reset and the new phase starts clean.
    spec_fingerprint: Optional[str] = None
    #: When a market fetch is failing, the time before which no further fetch is
    #: attempted. Persisted so a restart cannot reset the backoff.
    backoff_until: Optional[str] = None
    #: Set once the watcher reached a terminal state (invalidation or expiry).
    #: The loop stops polling terminal watchers.
    terminal: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "WatcherState":
        if not isinstance(data, dict):
            return cls()
        payload = dict(data)
        payload["alerted_conditions"] = dict(payload.get("alerted_conditions") or {})
        payload["fired_conditions"] = dict(payload.get("fired_conditions") or {})
        payload["escalated"] = bool(payload.get("escalated", False))
        payload["llm_reconfirmed"] = bool(payload.get("llm_reconfirmed", False))
        payload["terminal"] = bool(payload.get("terminal", False))
        payload["poll_count"] = int(payload.get("poll_count") or 0)
        payload["error_count"] = int(payload.get("error_count") or 0)
        return cls(**{key: payload.get(key) for key in cls.__dataclass_fields__})


@dataclass
class WatchItem:
    """A persistent deterministic market watcher."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: WatchStatus = WatchStatus.ACTIVE
    kind: WatchKind = WatchKind.WAITING_SETUP
    pair: str = ""
    asset_class: str = "crypto"
    direction: str = ""
    timeframe: str = ""
    #: Market-data source the watcher polls through (a ``VALID_SOURCES`` name,
    #: or ``"auto"`` to let the loader registry route by market). Defaults to
    #: ``yfinance``: the model picks a source deliberately, but an omitted one
    #: lands on the same venue the agent uses for ad-hoc quotes.
    source: str = "yfinance"
    entry_zone: Optional[PriceZone] = None
    waiting_trigger: str = ""
    trigger_rules: List[Dict[str, Any]] = field(default_factory=list)
    invalidation_conditions: List[str] = field(default_factory=list)
    invalidation_rules: List[Dict[str, Any]] = field(default_factory=list)
    expiry: Optional[str] = None
    session_window: Optional[str] = None
    source_session_id: Optional[str] = None
    source_attempt_id: Optional[str] = None
    session_id: Optional[str] = None
    thread_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    analysis_context: Dict[str, Any] = field(default_factory=dict)
    watcher_state: WatcherState = field(default_factory=WatcherState)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["kind"] = self.kind.value
        data["entry_zone"] = self.entry_zone.to_dict() if self.entry_zone else None
        data["watcher_state"] = self.watcher_state.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WatchItem":
        payload = dict(data)
        payload["status"] = WatchStatus(payload.get("status") or WatchStatus.ACTIVE.value)
        payload["kind"] = WatchKind(payload.get("kind") or WatchKind.WAITING_SETUP.value)
        payload["entry_zone"] = PriceZone.from_dict(payload.get("entry_zone"))
        payload["watcher_state"] = WatcherState.from_dict(payload.get("watcher_state"))
        payload["trigger_rules"] = list(payload.get("trigger_rules") or [])
        payload["invalidation_conditions"] = list(payload.get("invalidation_conditions") or [])
        payload["invalidation_rules"] = list(payload.get("invalidation_rules") or [])
        payload["analysis_context"] = dict(payload.get("analysis_context") or {})
        return cls(**payload)

    @property
    def is_pollable(self) -> bool:
        """Return whether this watcher should be polled."""
        return self.status == WatchStatus.ACTIVE and self.status not in TERMINAL_STATUSES

    def touch(self) -> None:
        """Update the watcher timestamp."""
        self.updated_at = utc_now_iso()


@dataclass
class WatcherEvent:
    """Append-only watcher event log entry."""

    watcher_id: str
    event_type: str
    message: str = ""
    condition_key: Optional[str] = None
    price: Optional[float] = None
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WatcherEvent":
        payload = dict(data)
        payload["data"] = dict(payload.get("data") or {})
        return cls(**payload)


#: Fields that define a watcher's *behaviour*. Presentation-only context
#: (``analysis_context``, ``waiting_trigger``, ``entry_zone`` used solely for
#: display) is deliberately excluded: a model that updates a grade or phase
#: after the fact must not reset fired-condition history, which would clear the
#: anti-spam guard and replay an alert the operator already saw.
def spec_fingerprint(item: "WatchItem") -> str:
    """Return a stable fingerprint of the watcher's rule set.

    When the model rewrites the rules to re-arm, the fingerprint changes and the
    loop resets the watcher's runtime state (fired conditions, cooldowns) so the
    previous phase's firing cannot suppress the new phase's rules.
    """
    return json.dumps(
        {
            "pair": item.pair,
            "asset_class": item.asset_class,
            "direction": item.direction,
            "timeframe": item.timeframe,
            "kind": item.kind.value if isinstance(item.kind, WatchKind) else str(item.kind),
            "trigger_rules": item.trigger_rules,
            "invalidation_rules": item.invalidation_rules,
            "entry_zone": item.entry_zone.to_dict() if item.entry_zone else None,
            "expiry": item.expiry,
        },
        sort_keys=True,
        default=str,
    )

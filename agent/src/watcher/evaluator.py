"""Deterministic watcher rule evaluation and anti-spam state transitions.

Pure with respect to the network and the LLM: every entry point takes an
explicit snapshot, and the clock is read only for stamping. The watch loop
calls :meth:`TriggerEvaluator.evaluate` with a market snapshot and persists
whatever it returns.

Ported from the Vibe-Trading (v0.1.6) watcher subsystem. The rule semantics,
anti-spam bookkeeping, and candle-close arming are deliberately identical so
operators get the behaviour they already trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.watcher.models import (
    WatchItem,
    WatchStatus,
    WatcherEvent,
    WatcherEventType,
    spec_fingerprint,
    utc_now_iso,
)


@dataclass(frozen=True)
class MarketSnapshot:
    """Latest market snapshot used by watcher rules."""

    pair: str
    price: float
    timestamp: str = field(default_factory=utc_now_iso)
    asset_class: str = "crypto"
    timeframe: Optional[str] = None
    candles: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class EvaluationResult:
    """Result of evaluating one watcher against one market snapshot."""

    watcher: WatchItem
    events: List[WatcherEvent] = field(default_factory=list)
    changed: bool = False


@dataclass(frozen=True)
class EvaluatorConfig:
    """Evaluator anti-spam knobs."""

    default_cooldown_seconds: int = 900
    default_approach_threshold_pct: float = 0.002


# ==================== time helpers ====================


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _timeframe_seconds(value: str) -> int:
    raw = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "1min": "1m",
        "1minute": "1m",
        "5min": "5m",
        "5minute": "5m",
        "15min": "15m",
        "15minute": "15m",
        "30min": "30m",
        "30minute": "30m",
        "1hr": "1h",
        "1hour": "1h",
        "4hr": "4h",
        "4hour": "4h",
    }
    normalized = aliases.get(raw, raw)
    if normalized.endswith("m"):
        try:
            return int(normalized[:-1]) * 60
        except ValueError:
            return 0
    if normalized.endswith("h"):
        try:
            return int(normalized[:-1]) * 3600
        except ValueError:
            return 0
    if normalized.endswith("d"):
        try:
            return int(normalized[:-1]) * 86400
        except ValueError:
            return 0
    return 0


def _candle_close_time(candle: Dict[str, Any], timeframe: str) -> Optional[datetime]:
    opened_at = _parse_dt(str(candle.get("timestamp") or ""))
    if opened_at is None:
        return None
    seconds = _timeframe_seconds(str(candle.get("timeframe") or timeframe))
    if seconds <= 0:
        return opened_at
    return opened_at + timedelta(seconds=seconds)


def _watcher_armed_at(item: WatchItem) -> Optional[datetime]:
    state = item.watcher_state
    stamps = [_parse_dt(getattr(state, "armed_at", None)), _parse_dt(item.created_at)]
    valid = [stamp for stamp in stamps if stamp is not None]
    return max(valid) if valid else None


# ==================== condition keys ====================


def _rule_key(event_type: str, rule: Dict[str, Any], index: int) -> str:
    rule_type = str(rule.get("type") or "rule").strip()
    if rule.get("id"):
        return f"{event_type}:{rule.get('id')}"
    if rule_type in {"price_enters_zone", "price_approaches_zone"}:
        return f"{event_type}:{rule_type}:{rule.get('low')}:{rule.get('high')}"
    if rule_type == "candle_close":
        return f"{event_type}:{rule_type}:{rule.get('timeframe', '')}:{rule.get('side', '')}:{rule.get('level')}"
    if "level" in rule:
        return f"{event_type}:{rule_type}:{rule.get('side', '')}:{rule.get('level')}"
    return f"{event_type}:{rule_type}:{index}"


# ==================== anti-spam ====================


def _cooldown_active(item: WatchItem, now: datetime) -> bool:
    cooldown_until = _parse_dt(item.watcher_state.cooldown_until)
    return bool(cooldown_until and cooldown_until > now)


def _mark_alerted(item: WatchItem, event: WatcherEvent, now: datetime, config: EvaluatorConfig) -> None:
    key = event.condition_key or event.event_type
    item.watcher_state.alerted_conditions[key] = event.created_at
    item.watcher_state.fired_conditions[key] = event.created_at
    item.watcher_state.last_event = event.event_type
    item.watcher_state.alert_sent_at = event.created_at
    item.watcher_state.cooldown_until = _iso(now + timedelta(seconds=config.default_cooldown_seconds))


def _event_already_sent(item: WatchItem, condition_key: str) -> bool:
    return condition_key in (item.watcher_state.alerted_conditions or {})


# ==================== level / zone helpers ====================


def _zone_bounds(rule: Dict[str, Any], item: WatchItem) -> Optional[tuple[float, float]]:
    low = rule.get("low")
    high = rule.get("high")
    if low is None or high is None:
        if item.entry_zone is None:
            return None
        low = item.entry_zone.low
        high = item.entry_zone.high
    try:
        low_f = float(low)
        high_f = float(high)
    except (TypeError, ValueError):
        return None
    return (min(low_f, high_f), max(low_f, high_f))


def _distance_to_zone(price: float, low: float, high: float) -> float:
    if low <= price <= high:
        return 0.0
    if price < low:
        return low - price
    return price - high


def _approach_threshold(rule: Dict[str, Any], price: float, config: EvaluatorConfig) -> float:
    if rule.get("threshold") is not None:
        try:
            return abs(float(rule["threshold"]))
        except (TypeError, ValueError):
            pass
    threshold_pct = rule.get("threshold_pct", config.default_approach_threshold_pct)
    try:
        return abs(float(price) * float(threshold_pct))
    except (TypeError, ValueError):
        return abs(float(price) * config.default_approach_threshold_pct)


def _crossed_level(previous: Optional[float], current: float, level: float, side: str) -> bool:
    if previous is None:
        return False
    if side == "above":
        return current >= level and previous < level
    if side == "below":
        return current <= level and previous > level
    return False


def _breached_level(current: float, level: float, side: str) -> bool:
    if side == "above":
        return current >= level
    if side == "below":
        return current <= level
    return False


# ==================== candle helpers ====================


def _confirmed_candles(snapshot: MarketSnapshot, timeframe: str = "") -> List[Dict[str, Any]]:
    expected = str(timeframe or "").strip().lower()
    candles: List[Dict[str, Any]] = []
    for candle in snapshot.candles or []:
        if not isinstance(candle, dict):
            continue
        if candle.get("confirmed") is False:
            continue
        candle_timeframe = str(candle.get("timeframe") or "").strip().lower()
        if expected and candle_timeframe and candle_timeframe != expected:
            continue
        if candle.get("close") is None:
            continue
        candles.append(candle)
    return candles


def _matching_candles(snapshot: MarketSnapshot, timeframe: str = "") -> List[Dict[str, Any]]:
    expected = str(timeframe or "").strip().lower()
    candles: List[Dict[str, Any]] = []
    for candle in snapshot.candles or []:
        if not isinstance(candle, dict):
            continue
        candle_timeframe = str(candle.get("timeframe") or "").strip().lower()
        if expected and candle_timeframe and candle_timeframe != expected:
            continue
        if candle.get("close") is None:
            continue
        candles.append(candle)
    return candles


def _primary_candle_timeframe(item: WatchItem, snapshot: MarketSnapshot) -> str:
    for rule in [*(item.trigger_rules or []), *(item.invalidation_rules or [])]:
        if not isinstance(rule, dict):
            continue
        if str(rule.get("type") or "").strip().lower() != "candle_close":
            continue
        timeframe = str(rule.get("timeframe") or "").strip()
        if timeframe:
            return timeframe
    return str(item.timeframe or snapshot.timeframe or "").strip()


def _update_candle_state(item: WatchItem, snapshot: MarketSnapshot) -> None:
    timeframe = _primary_candle_timeframe(item, snapshot)
    candles = _matching_candles(snapshot, timeframe)
    confirmed = [candle for candle in candles if candle.get("confirmed") is not False]
    item.watcher_state.last_candle = candles[-1] if candles else None
    item.watcher_state.last_confirmed_candle = confirmed[-1] if confirmed else None
    item.watcher_state.previous_confirmed_candle = confirmed[-2] if len(confirmed) >= 2 else None


def _candle_close_cross(snapshot: MarketSnapshot, rule: Dict[str, Any], item: WatchItem) -> tuple[bool, Dict[str, Any] | None]:
    try:
        level = float(rule["level"])
    except (KeyError, TypeError, ValueError):
        return False, None
    side = str(rule.get("side") or "").strip().lower()
    if side not in {"above", "below"}:
        side = "below" if item.direction.upper() == "SHORT" else "above"
    timeframe = str(rule.get("timeframe") or item.timeframe or snapshot.timeframe or "").strip().lower()
    candles = _confirmed_candles(snapshot, timeframe)
    if not candles:
        return False, None
    armed_at = _watcher_armed_at(item)
    matched: Dict[str, Any] | None = None
    for index, current in enumerate(candles):
        current_close_time = _candle_close_time(current, timeframe)
        if armed_at is not None and current_close_time is not None and current_close_time <= armed_at:
            continue
        try:
            current_close = float(current["close"])
        except (TypeError, ValueError):
            continue
        if not _breached_level(current_close, level, side):
            continue
        previous = candles[index - 1] if index > 0 else None
        previous_close = None
        if previous is not None:
            try:
                previous_close = float(previous["close"])
            except (TypeError, ValueError):
                previous_close = None
        matched = {
            "timeframe": timeframe or None,
            "level": level,
            "side": side,
            "previous_candle": previous,
            "current_candle": current,
            "previous_close": previous_close,
            "current_close": current_close,
        }
        break
    return bool(matched), matched


class TriggerEvaluator:
    """Evaluate deterministic watcher rules without invoking an LLM."""

    def __init__(self, config: Optional[EvaluatorConfig] = None) -> None:
        self.config = config or EvaluatorConfig()

    def _reset_for_rearm(self, item: WatchItem, now: datetime) -> None:
        """Clear firing history when the authored rule set changed.

        The model re-arms a watcher by rewriting its rules. Without this the
        previous phase's ``alerted_conditions`` would suppress the new phase's
        rules and the watcher would never fire again.
        """
        item.watcher_state.alerted_conditions = {}
        item.watcher_state.fired_conditions = {}
        item.watcher_state.cooldown_until = None
        item.watcher_state.escalated = False
        item.watcher_state.armed_at = _iso(now)
        item.watcher_state.terminal = False

    def evaluate(self, item: WatchItem, snapshot: MarketSnapshot) -> EvaluationResult:
        """Evaluate a watcher and mutate its state in memory.

        The caller is responsible for persisting the returned watcher and events.
        """
        result = EvaluationResult(watcher=item)
        now = _parse_dt(snapshot.timestamp) or _now()

        fingerprint = spec_fingerprint(item)
        if item.watcher_state.spec_fingerprint != fingerprint:
            self._reset_for_rearm(item, now)
            item.watcher_state.spec_fingerprint = fingerprint

        previous_price = item.watcher_state.last_price
        item.watcher_state.poll_count += 1
        item.watcher_state.last_checked = utc_now_iso()
        item.watcher_state.last_market_timestamp = _iso(now)
        item.watcher_state.last_price = float(snapshot.price)
        _update_candle_state(item, snapshot)
        result.changed = True

        if item.status != WatchStatus.ACTIVE:
            return result

        expiry = _parse_dt(item.expiry)
        if expiry and now >= expiry:
            event = WatcherEvent(
                watcher_id=item.id,
                event_type=WatcherEventType.EXPIRED.value,
                condition_key="EXPIRED:expiry",
                price=float(snapshot.price),
                message=f"{item.pair} watcher expired.",
                data={"expiry": item.expiry, "current_price": snapshot.price},
                created_at=_iso(now),
            )
            if not _event_already_sent(item, event.condition_key):
                result.events.append(event)
                _mark_alerted(item, event, now, self.config)
            item.status = WatchStatus.EXPIRED
            item.watcher_state.terminal = True
            return result

        invalidation_event = self._first_invalidation(item, snapshot, previous_price, now)
        if invalidation_event:
            result.events.append(invalidation_event)
            _mark_alerted(item, invalidation_event, now, self.config)
            item.status = WatchStatus.INVALIDATED
            item.watcher_state.terminal = True
            return result

        trigger_hit = self._first_trigger_hit(item, snapshot, previous_price, now)
        if trigger_hit:
            result.events.append(trigger_hit)
            _mark_alerted(item, trigger_hit, now, self.config)
            item.status = WatchStatus.TRIGGERED
            item.watcher_state.escalated = True
            item.watcher_state.terminal = True
            return result

        trigger_close = self._first_trigger_close(item, snapshot, now)
        if trigger_close and not _cooldown_active(item, now):
            result.events.append(trigger_close)
            _mark_alerted(item, trigger_close, now, self.config)

        management_alert = self._first_management_alert(item, snapshot, previous_price, now)
        if management_alert and not _cooldown_active(item, now):
            result.events.append(management_alert)
            _mark_alerted(item, management_alert, now, self.config)

        return result

    def _first_invalidation(
        self,
        item: WatchItem,
        snapshot: MarketSnapshot,
        previous_price: Optional[float],
        now: datetime,
    ) -> Optional[WatcherEvent]:
        for index, rule in enumerate(item.invalidation_rules):
            rule_type = str(rule.get("type") or "").strip()
            if rule_type == "candle_close":
                breached, candle_data = _candle_close_cross(snapshot, rule, item)
                if not breached:
                    continue
                condition_key = _rule_key(WatcherEventType.INVALIDATED.value, rule, index)
                if _event_already_sent(item, condition_key):
                    continue
                return WatcherEvent(
                    watcher_id=item.id,
                    event_type=WatcherEventType.INVALIDATED.value,
                    condition_key=condition_key,
                    price=float(snapshot.price),
                    message=f"{item.pair} invalidated: {candle_data.get('timeframe') or item.timeframe} candle closed {candle_data.get('side')} {candle_data.get('level')}.",
                    data={"rule": rule, "current_price": snapshot.price, **(candle_data or {})},
                    created_at=_iso(now),
                )
            if rule_type not in {"price_breaks_level", "price_crosses_level"}:
                continue
            try:
                level = float(rule["level"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(rule.get("side") or rule.get("direction") or "").strip().lower()
            if rule_type == "price_crosses_level":
                breached = _crossed_level(previous_price, float(snapshot.price), level, side)
            else:
                breached = _breached_level(float(snapshot.price), level, side)
            if not breached:
                continue
            condition_key = _rule_key(WatcherEventType.INVALIDATED.value, rule, index)
            if _event_already_sent(item, condition_key):
                continue
            return WatcherEvent(
                watcher_id=item.id,
                event_type=WatcherEventType.INVALIDATED.value,
                condition_key=condition_key,
                price=float(snapshot.price),
                message=f"{item.pair} invalidated: price {side} {level}.",
                data={"rule": rule, "previous_price": previous_price, "current_price": snapshot.price},
                created_at=_iso(now),
            )
        return None

    def _first_trigger_hit(
        self,
        item: WatchItem,
        snapshot: MarketSnapshot,
        previous_price: Optional[float],
        now: datetime,
    ) -> Optional[WatcherEvent]:
        for index, rule in enumerate(item.trigger_rules):
            rule_type = str(rule.get("type") or "").strip()
            hit = False
            candle_data: Dict[str, Any] | None = None
            if rule_type == "price_enters_zone":
                bounds = _zone_bounds(rule, item)
                if bounds is None:
                    continue
                low, high = bounds
                current = float(snapshot.price)
                previous_inside = previous_price is not None and low <= float(previous_price) <= high
                current_inside = low <= current <= high
                hit = current_inside and previous_price is not None and not previous_inside
            elif rule_type == "candle_close":
                hit, candle_data = _candle_close_cross(snapshot, rule, item)
            elif rule_type in {"price_crosses_level", "level_reclaim", "level_breakdown", "price_breaks_level"}:
                try:
                    level = float(rule["level"])
                except (KeyError, TypeError, ValueError):
                    continue
                side = str(rule.get("side") or rule.get("direction") or "").strip().lower()
                if not side:
                    side = "above" if rule_type == "level_reclaim" else "below" if rule_type == "level_breakdown" else ""
                if rule_type == "price_breaks_level":
                    hit = _breached_level(float(snapshot.price), level, side)
                else:
                    hit = _crossed_level(previous_price, float(snapshot.price), level, side)
            elif rule_type == "liquidity_sweep_touched":
                try:
                    level = float(rule["level"])
                except (KeyError, TypeError, ValueError):
                    continue
                side = str(rule.get("side") or "").strip().lower()
                hit = _breached_level(float(snapshot.price), level, side)
            if not hit:
                continue
            condition_key = _rule_key(WatcherEventType.TRIGGER_HIT.value, rule, index)
            if _event_already_sent(item, condition_key):
                continue
            return WatcherEvent(
                watcher_id=item.id,
                event_type=WatcherEventType.TRIGGER_HIT.value,
                condition_key=condition_key,
                price=float(snapshot.price),
                message=(
                    f"{item.pair} {candle_data.get('timeframe')} candle closed {candle_data.get('side')} {candle_data.get('level')}."
                    if rule_type == "candle_close" and candle_data
                    else f"{item.pair} trigger hit at {snapshot.price}."
                ),
                data={"rule": rule, "previous_price": previous_price, "current_price": snapshot.price, **(candle_data or {})},
                created_at=_iso(now),
            )
        return None

    def _first_trigger_close(
        self,
        item: WatchItem,
        snapshot: MarketSnapshot,
        now: datetime,
    ) -> Optional[WatcherEvent]:
        for index, rule in enumerate(item.trigger_rules):
            rule_type = str(rule.get("type") or "").strip()
            if rule_type not in {"price_enters_zone", "price_approaches_zone"}:
                continue
            bounds = _zone_bounds(rule, item)
            if bounds is None:
                continue
            low, high = bounds
            if low <= float(snapshot.price) <= high:
                continue
            distance = _distance_to_zone(float(snapshot.price), low, high)
            threshold = _approach_threshold(rule, float(snapshot.price), self.config)
            if distance > threshold:
                continue
            condition_key = _rule_key(WatcherEventType.TRIGGER_CLOSE.value, rule, index)
            if _event_already_sent(item, condition_key):
                continue
            return WatcherEvent(
                watcher_id=item.id,
                event_type=WatcherEventType.TRIGGER_CLOSE.value,
                condition_key=condition_key,
                price=float(snapshot.price),
                message=f"{item.pair} is approaching entry zone {low}-{high}.",
                data={
                    "rule": rule,
                    "current_price": snapshot.price,
                    "zone": {"low": low, "high": high},
                    "distance": distance,
                    "threshold": threshold,
                },
                created_at=_iso(now),
            )
        return None

    def _first_management_alert(
        self,
        item: WatchItem,
        snapshot: MarketSnapshot,
        previous_price: Optional[float],
        now: datetime,
    ) -> Optional[WatcherEvent]:
        if item.kind.value != "active_trade":
            return None
        rules = list(item.trigger_rules or [])
        for index, rule in enumerate(rules):
            rule_type = str(rule.get("type") or "").strip()
            if rule_type not in {
                "take_profit_level",
                "break_even_level",
                "invalidation_approach",
                "management_level",
            }:
                continue
            try:
                level = float(rule["level"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(rule.get("side") or "").strip().lower()
            if not side:
                direction = str(item.direction or "").strip().upper()
                side = "above" if direction == "LONG" else "below" if direction == "SHORT" else ""
            if rule_type == "invalidation_approach":
                threshold = _approach_threshold(rule, float(snapshot.price), self.config)
                if abs(float(snapshot.price) - level) > threshold:
                    continue
            elif not _crossed_level(previous_price, float(snapshot.price), level, side):
                continue
            condition_key = _rule_key(WatcherEventType.MANAGEMENT_ALERT.value, rule, index)
            if _event_already_sent(item, condition_key):
                continue
            label = str(rule.get("label") or rule_type).replace("_", " ").upper()
            return WatcherEvent(
                watcher_id=item.id,
                event_type=WatcherEventType.MANAGEMENT_ALERT.value,
                condition_key=condition_key,
                price=float(snapshot.price),
                message=f"{item.pair} management alert: {label} at {level}.",
                data={"rule": rule, "previous_price": previous_price, "current_price": snapshot.price},
                created_at=_iso(now),
            )
        return None

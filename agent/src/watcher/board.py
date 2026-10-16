"""Market-watcher board rendering.

Pure port of OpenAlice's ``packages/connector-protocol/src/watcher-board.ts``,
which was itself ported from Vibe-Trading's ``discord_bot.py`` watcher board.
The bar grammar (filled/empty blocks), the arrow layout and the pulse
thresholds are kept identical so an operator gets the readout they already
trust.

Pure: no I/O, no clock of its own (the caller passes ``now_iso``), no imports
from the Telegram layer. This lets the board be built once and rendered by any
surface (Telegram today, the Web UI later), and tested without a bot.

The layout has two bodies per watcher:

* ``card``   - the compact one-message card shown collapsed.
* ``detail`` - the full panel shown when the card is expanded.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ==================== primitives ====================

#: Bar width, matching OpenAlice / the original discord bot board.
BAR_WIDTH = 12
#: Filled / empty block glyphs.
FILLED = "\u25b0"
EMPTY = "\u25b1"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware UTC datetime, or None."""
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


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def watcher_timeframe_seconds(value: Any) -> int:
    """``1m`` / ``15m`` / ``4h`` / ``1d`` / ``1w`` -> seconds. 0 when unparseable."""
    raw = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "1min": "1m", "1minute": "1m", "5min": "5m", "5minute": "5m",
        "15min": "15m", "15minute": "15m", "30min": "30m", "30minute": "30m",
        "1hr": "1h", "1hour": "1h", "4hr": "4h", "4hour": "4h",
        "1day": "1d", "daily": "1d",
    }
    normalized = aliases.get(raw, raw)
    match = re.fullmatch(r"(\d+)([mhdwM])", normalized)
    if not match:
        return 0
    count = int(match.group(1))
    if count <= 0:
        return 0
    unit = match.group(2)
    factors = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "M": 30 * 86400}
    return count * factors.get(unit, 0)


def format_duration(seconds: float) -> str:
    """``95s`` / ``4m 12s`` / ``2h 3m`` - compact, never ``0h 0m 5s``."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        rest = total % 60
        return f"{minutes}m" if rest == 0 else f"{minutes}m {rest}s"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if hours < 24:
        return f"{hours}h" if rest_minutes == 0 else f"{hours}h {rest_minutes}m"
    days = hours // 24
    rest_hours = hours % 24
    return f"{days}d" if rest_hours == 0 else f"{days}d {rest_hours}h"


def format_price_value(value: Any) -> str:
    """Prices keep their significant digits but never trailing zeros."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return "-"
    text = f"{number:.5f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def requirement_bar(percent: Any, width: int = BAR_WIDTH) -> str:
    """A filled bar that never fully fills until 100."""
    try:
        pct = max(0, min(100, round(float(percent))))
    except (TypeError, ValueError):
        pct = 0
    filled = round((pct / 100) * width)
    if pct > 0 and filled == 0:
        filled = 1
    if pct < 100:
        filled = min(filled, max(0, width - 1))
    return FILLED * filled + EMPTY * max(0, width - filled)


def watcher_progress_bar(frame: int, width: int = BAR_WIDTH) -> str:
    """Animated pulse used while the poller is alive."""
    lit = (max(0, int(frame)) % width) + 1
    return FILLED * lit + EMPTY * max(0, width - lit)


def status_dot(frame: int) -> str:
    return "\U0001f7e2" if max(0, int(frame)) % 2 == 0 else "\u26ab"


def watcher_pulse_bar(
    *,
    frame: int,
    running: bool,
    pollable: bool,
    age_seconds: Optional[float],
    poll_seconds: float,
) -> Tuple[str, str]:
    """Liveness readout: icon + bar + honest wording.

    A watcher that has not ticked inside its cadence is ``delayed``, and past 5x
    it is ``stale`` - so a stalled loop cannot look healthy.
    """
    if not running:
        return "\U0001f534", f"{EMPTY * BAR_WIDTH} poller stopped"
    if not pollable:
        return "\U0001f7e1", f"{FILLED * 3}{EMPTY * (BAR_WIDTH - 3)} not polling this setup"
    if age_seconds is None:
        return "\U0001f7e1", f"{watcher_progress_bar(frame)} waiting for first tick"
    expected = max(1.0, float(poll_seconds))
    if age_seconds <= expected * 2:
        return status_dot(frame), f"{watcher_progress_bar(frame)} live \u00b7 last tick {format_duration(age_seconds)} ago"
    if age_seconds <= expected * 5:
        return "\U0001f7e0", f"{FILLED * 6}{EMPTY * (BAR_WIDTH - 6)} delayed \u00b7 last tick {format_duration(age_seconds)} ago"
    return "\U0001f534", f"{FILLED * 2}{EMPTY * (BAR_WIDTH - 2)} stale \u00b7 last tick {format_duration(age_seconds)} ago"


def truncate(text: Any, limit: int) -> str:
    """Trim and clip to ``limit`` characters, adding an ellipsis when cut."""
    trimmed = str(text or "").strip()
    if len(trimmed) <= limit:
        return trimmed
    return trimmed[: max(0, limit - 1)].rstrip() + "\u2026"


# ==================== trigger geometry ====================


def _spec_rules(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = spec.get("rules")
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _invalidation_rules(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = spec.get("invalidationRules")
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def _management_rules(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    rules = spec.get("managementRules")
    return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []


def primary_rule(spec: Dict[str, Any]) -> Dict[str, Any]:
    rules = _spec_rules(spec)
    return rules[0] if rules else {"type": "rule"}


def is_candle_close_trigger(spec: Dict[str, Any]) -> bool:
    """True when the deciding rule is a confirmed ``candle_close``."""
    return str(primary_rule(spec).get("type") or "").strip().lower() == "candle_close"


def zone_bounds(spec: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """The entry zone, from a spec-level ``entryZone`` or whichever rule authored one."""
    raw_zone = spec.get("entryZone")
    if isinstance(raw_zone, dict):
        low, high = raw_zone.get("low"), raw_zone.get("high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            left, right = float(low), float(high)
            return (left, right) if left <= right else (right, left)
    for rule in [*_spec_rules(spec), *_invalidation_rules(spec)]:
        low = rule.get("low")
        high = rule.get("high")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            left, right = float(low), float(high)
            return (left, right) if left <= right else (right, left)
    return None


def zone_position(price: Optional[float], zone: Optional[Tuple[float, float]]) -> str:
    """Where price sits relative to the entry zone.

    ``inside entry zone (x)`` / ``<d> above zone`` / ``<d> below zone``; ``-``
    when there is no usable zone. Mirrors Vibe-Trading's ``_zone_position``.
    """
    if price is None or zone is None:
        return "-"
    low, high = zone
    if low <= price <= high:
        return f"inside entry zone ({format_price_value(price)})"
    if price > high:
        return f"{format_price_value(price - high)} above zone"
    return f"{format_price_value(low - price)} below zone"


def watcher_trigger_target(spec: Dict[str, Any]) -> Tuple[str, str]:
    """Where the trigger sits: ``[target, human_text]``.

    A rule with a level wins; otherwise the entry zone, otherwise the authored
    trigger prose.
    """
    rules = _spec_rules(spec)
    zone = zone_bounds(spec)
    if not rules:
        prose = str(spec.get("waitingTrigger") or "-")
        if zone:
            return f"{format_price_value(zone[0])}-{format_price_value(zone[1])}", prose
        return "-", prose
    rule = rules[0]
    rule_type = str(rule.get("type") or "rule").strip().lower()
    side = str(rule.get("side") or "").strip()
    level = rule.get("level")
    if isinstance(level, (int, float)) and not isinstance(level, bool):
        if not side and rule_type == "level_reclaim":
            side = "above"
        elif not side and rule_type == "level_breakdown":
            side = "below"
        elif not side and rule_type == "candle_close":
            side = "below" if spec.get("direction") == "SHORT" else "above"
        label = "above" if side == "above" else "below" if side == "below" else (side or "at")
        timeframe = str(rule.get("timeframe") or spec.get("timeframe") or "").strip()
        if rule_type == "candle_close":
            prefix = f"{timeframe} candle close" if timeframe else "candle close"
            return str(level), f"{prefix} {label} {level}"
        suffix = f" on {timeframe}" if timeframe else ""
        return str(level), f"price {label} {level}{suffix}"
    if rule_type in {"price_enters_zone", "price_approaches_zone", "zone", "entry_zone"}:
        low = rule.get("low", zone[0] if zone else None)
        high = rule.get("high", zone[1] if zone else None)
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            return f"{low}-{high}", f"price enters {low}-{high}"
    return "-", str(spec.get("waitingTrigger") or rule_type)


def watcher_trigger_side(spec: Dict[str, Any]) -> str:
    """Which side of the level counts as the trigger, defaulting from direction."""
    rule = primary_rule(spec)
    rule_type = str(rule.get("type") or "").strip().lower()
    side = str(rule.get("side") or "").strip().lower()
    if side in {"above", "below"}:
        return side
    if rule_type == "level_reclaim":
        return "above"
    if rule_type == "level_breakdown":
        return "below"
    direction = str(spec.get("direction") or "").strip().upper()
    level = rule.get("level")
    if direction == "SHORT" and isinstance(level, (int, float)):
        return "below"
    if direction == "LONG" and isinstance(level, (int, float)):
        return "above"
    return ""


def candle_close_basis(
    spec: Dict[str, Any], state: Dict[str, Any], fallback: Optional[float]
) -> Optional[float]:
    """Basis price for a candle trigger; the live price otherwise."""
    if not is_candle_close_trigger(spec):
        return fallback
    if isinstance(fallback, (int, float)) and not isinstance(fallback, bool):
        return float(fallback)
    candle = state.get("lastConfirmedCandle")
    if isinstance(candle, dict) and isinstance(candle.get("close"), (int, float)):
        return float(candle["close"])
    return None


def _trigger_crossed(price: Optional[float], target: str, side: str) -> bool:
    if price is None:
        return False
    try:
        level = float(target)
    except (TypeError, ValueError):
        return False
    if side == "below":
        return price <= level
    if side == "above":
        return price >= level
    return False


def _confirmed_candle_crossed(spec: Dict[str, Any], state: Dict[str, Any], target: str, side: str) -> bool:
    if not is_candle_close_trigger(spec):
        return False
    candle = state.get("lastConfirmedCandle")
    if not isinstance(candle, dict) or not isinstance(candle.get("close"), (int, float)):
        return False
    return _trigger_crossed(float(candle["close"]), target, side)


def _clamp_percent(value: float) -> int:
    if value != value:
        return 0
    return max(0, min(99, round(value)))


def _anchor_for_side(side: str, level: float, anchor: Optional[float]) -> Optional[float]:
    if anchor is None or anchor != anchor or anchor == level:
        return None
    if side == "below":
        return anchor if anchor > level else None
    return anchor if anchor < level else None


def _fallback_percent(
    zone: Optional[Tuple[float, float]], level: float, price: float, anchor: Optional[float]
) -> int:
    if anchor is not None:
        total = abs(level - anchor)
        if total > 0:
            remaining = abs(level - price)
            return _clamp_percent((1 - remaining / total) * 100)
    width = abs(zone[1] - zone[0]) if zone else 0
    reference = max(width, abs(level) * 0.001, 0.00001)
    return _clamp_percent((1 - abs(level - price) / reference) * 100)


def watcher_trigger_progress(
    spec: Dict[str, Any],
    state: Dict[str, Any],
    live_price: Optional[float],
    target: str,
    side: str,
) -> Dict[str, Any]:
    """How close the market is to the trigger: percent plus the two sentences.

    Distance is measured from the near edge of the entry zone when one exists,
    so the bar communicates real progress rather than a raw ratio to zero.
    """
    price = candle_close_basis(spec, state, live_price)
    if price is None:
        return {"percent": 0, "text": "progress unavailable", "need": "need current price", "basis": None}

    zone = zone_bounds(spec)
    # A hyphenated target (`113-115.6`) is the ZONE marker, not a number. Detect
    # it BEFORE float(): `float('113-115.6')` raises, but a partial parse would
    # silently route a zone watcher into the scalar branch.
    zone_target = target == "-" or "-" in target

    if zone_target:
        if zone is None:
            return {"percent": 0, "text": "progress unavailable", "need": "need a valid entry zone", "basis": price}
        low, high = zone
        if low <= price <= high:
            return {"percent": 100, "text": "entry zone reached", "need": "price is inside the watched zone", "basis": price}
        distance = low - price if price < low else price - high
        width = max(0.00001, high - low)
        reference = max(width, abs(price) * 0.005, 0.00001)
        percent = _clamp_percent((1 - distance / reference) * 100)
        text = f"{percent}% to entry zone" if price < low else f"{percent}% back to entry zone"
        need = f"need +{format_price_value(distance)}" if price < low else f"need -{format_price_value(distance)}"
        return {"percent": percent, "text": text, "need": need, "basis": price}

    try:
        numeric_target = float(target)
    except (TypeError, ValueError):
        return {"percent": 0, "text": "progress unavailable", "need": "need a numeric trigger target", "basis": price}

    # A candle watcher fires only on a CONFIRMED close, so a live price beyond
    # the level is a FORMING candle, not a fired trigger. Checked BEFORE the
    # 100% returns below: reporting `trigger reached` for a candle that has not
    # closed claims a fire that never happened.
    if is_candle_close_trigger(spec) and _trigger_crossed(price, target, side):
        if _confirmed_candle_crossed(spec, state, target, side):
            return {
                "percent": 100,
                "text": "confirmed candle trigger reached",
                "need": "price is at or below trigger" if side == "below" else "price is at or above trigger",
                "basis": price,
            }
        return {"percent": 99, "text": "forming candle beyond trigger", "need": "needs confirmed candle close", "basis": price}

    zone_edge = (zone[1] if side == "below" else zone[0]) if zone else None
    confirmed_close = state.get("lastConfirmedCandle")
    raw_anchor = None
    if isinstance(confirmed_close, dict) and isinstance(confirmed_close.get("close"), (int, float)):
        raw_anchor = float(confirmed_close["close"])
    anchor = _anchor_for_side(side, numeric_target, raw_anchor)

    if side == "below":
        if price <= numeric_target:
            return {"percent": 100, "text": "breakdown trigger reached", "need": "price is at or below trigger", "basis": price}
        start = zone_edge if (zone_edge is not None and zone_edge > numeric_target) else None
        percent = (
            _fallback_percent(zone, numeric_target, price, anchor)
            if start is None
            else _clamp_percent(((start - price) / max(0.00001, start - numeric_target)) * 100)
        )
        text = f"{percent}% to breakdown"
        need = f"need -{format_price_value(price - numeric_target)}"
    else:
        if price >= numeric_target:
            return {"percent": 100, "text": "trigger reached", "need": "price is at or above trigger", "basis": price}
        start = zone_edge if (zone_edge is not None and zone_edge < numeric_target) else None
        percent = (
            _fallback_percent(zone, numeric_target, price, anchor)
            if start is None
            else _clamp_percent(((price - start) / max(0.00001, numeric_target - start)) * 100)
        )
        text = f"{percent}% to trigger"
        need = f"need +{format_price_value(numeric_target - price)}"

    if is_candle_close_trigger(spec):
        if "to breakdown" in text:
            text = text.replace("to breakdown", "to confirmed breakdown")
        elif "to trigger" in text:
            text = text.replace("to trigger", "to confirmed close")
    return {"percent": percent, "text": text, "need": need, "basis": price}


def distance_to_trigger(
    price: Optional[float], target: str, zone: Optional[Tuple[float, float]], side: str
) -> str:
    """``0.00123 below trigger`` / ``inside target zone`` - the raw distance line."""
    if price is None or not target or target == "-":
        return "-"
    if "-" in target:
        if zone is None:
            return "-"
        low, high = zone
        if low <= price <= high:
            return "inside target zone"
        if price < low:
            return f"{format_price_value(low - price)} below zone"
        return f"{format_price_value(price - high)} above zone"
    try:
        numeric_target = float(target)
    except (TypeError, ValueError):
        return "-"
    distance = numeric_target - price
    if abs(distance) < 0.00001:
        return "at target"
    if side == "below":
        label = "above breakdown" if price > numeric_target else "below breakdown"
    elif side == "above":
        label = "below trigger" if price < numeric_target else "above trigger"
    else:
        label = "below target" if distance > 0 else "above target"
    return f"{format_price_value(abs(distance))} {label}"


def watcher_candle_tracking_line(spec: Dict[str, Any], state: Dict[str, Any], target: str, side: str) -> str:
    """Candle bookkeeping line for a ``candle_close`` watcher. Empty otherwise."""
    if not is_candle_close_trigger(spec):
        return ""
    rule = primary_rule(spec)
    timeframe = str(rule.get("timeframe") or spec.get("timeframe") or "").strip()
    timeframe_text = f"{timeframe} " if timeframe else ""
    label = "above" if side == "above" else "below" if side == "below" else "at"
    try:
        level = float(target)
    except (TypeError, ValueError):
        level = None
    last_candle = state.get("lastConfirmedCandle")
    previous = state.get("previousConfirmedCandle")
    if isinstance(last_candle, dict) and isinstance(last_candle.get("close"), (int, float)) and last_candle.get("confirmed") is False:
        confirmed_close = format_price_value(previous.get("close")) if isinstance(previous, dict) else "-"
        return (
            f"\U0001f56f\ufe0f **Candle tracking**: forming `{timeframe_text}close {format_price_value(last_candle['close'])}` "
            f"\u00b7 last confirmed `{confirmed_close}` \u00b7 waits for confirmed close {label} `{format_price_value(level)}`"
        )
    if isinstance(last_candle, dict) and isinstance(last_candle.get("close"), (int, float)):
        return (
            f"\U0001f56f\ufe0f **Candle tracking**: last confirmed `{timeframe_text}close {format_price_value(last_candle['close'])}` "
            f"\u00b7 waits for confirmed close {label} `{format_price_value(level)}`"
        )
    return f"\U0001f56f\ufe0f **Candle tracking**: waiting for `{timeframe_text}confirmed close` data"


def watcher_candle_countdown_line(spec: Dict[str, Any], state: Dict[str, Any], now_iso: str) -> str:
    """Time left in the candle that will decide a ``candle_close`` watcher."""
    if not is_candle_close_trigger(spec):
        return ""
    rule = primary_rule(spec)
    timeframe = str(rule.get("timeframe") or spec.get("timeframe") or "").strip()
    seconds = watcher_timeframe_seconds(timeframe)
    if seconds <= 0:
        return ""
    now = _parse_iso(now_iso) or _now()
    market = _parse_iso(state.get("lastMarketTimestamp"))
    anchor = market or now
    elapsed = int(anchor.timestamp()) % seconds
    remaining = seconds - (elapsed or seconds)
    if market is not None:
        drift = max(0, int((now - market).total_seconds()))
        remaining = max(0, remaining - drift)
    if remaining <= 0:
        return f"\u23f3 **Candle timer**: `{timeframe}` close is due now \u00b7 waiting for confirmed candle data"
    return f"\u23f3 **Candle timer**: `{timeframe}` closes in `{format_duration(remaining)}`"


# ==================== labels ====================

RULE_LABELS = {
    "price_breaks_level": "Price breaks level",
    "price_crosses_level": "Price crosses level",
    "candle_close": "Candle close",
    "level_reclaim": "Level reclaim",
    "level_breakdown": "Level breakdown",
    "liquidity_sweep_touched": "Liquidity sweep",
    "price_enters_zone": "Price enters zone",
    "price_approaches_zone": "Price approaches zone",
    "r_multiple_reached": "R multiple",
    "unrealized_pnl_pct": "Unrealized P&L",
}


def describe_rule(rule: Dict[str, Any], fallback_timeframe: Optional[str] = None) -> str:
    """Human label for one rule, with its discriminating fields folded in."""
    rule_type = str(rule.get("type") or "rule")
    label = RULE_LABELS.get(rule_type, rule_type)
    if rule_type == "candle_close":
        tf = rule.get("timeframe") or fallback_timeframe or ""
        return f"{label} \u00b7 {tf} {rule.get('side') or ''} {rule.get('level') or ''}".strip()
    if rule_type in {"price_enters_zone", "price_approaches_zone"}:
        return f"{label} \u00b7 {rule.get('low', '?')}\u2013{rule.get('high', '?')}"
    if rule_type == "r_multiple_reached":
        return f"{label} \u00b7 {rule.get('multiple', '?')}R"
    if rule_type == "unrealized_pnl_pct":
        return f"{label} \u00b7 {rule.get('op') or ''} {rule.get('pct', '?')}%".strip()
    if isinstance(rule.get("level"), (int, float)):
        return f"{label} \u00b7 {rule.get('side') or ''} {rule['level']}".strip()
    return label


def _targets_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(format_price_value(item) for item in value) if value else "-"
    if isinstance(value, str):
        return value.strip() or "-"
    return "-"


def _event_time_or_price(event: Dict[str, Any]) -> str:
    price = event.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        return f"price `{format_price_value(price)}`"
    parsed = _parse_iso(event.get("at"))
    if parsed is not None:
        return f"time `{parsed.strftime('%H:%M:%S')} UTC`"
    return "time `-`"


# ==================== card renderer ====================


def render_watcher_board_card(
    spec: Dict[str, Any],
    state: Dict[str, Any],
    *,
    watcher_id: str,
    running: bool,
    pollable: bool,
    poll_seconds: float,
    now_iso: str,
    meta: Optional[Dict[str, Any]] = None,
    events: Optional[Sequence[Dict[str, Any]]] = None,
    workspace_label: str = "",
    frame: int = 0,
) -> Dict[str, Any]:
    """Render one watcher into the two chat bodies the board sends.

    Returns a dict with ``card`` (compact) and ``detail`` (expanded) plus the
    derived ``progress_percent`` and ``terminal``. Pure and total: a watcher
    with no state yet renders a ``waiting for first tick`` card rather than
    raising, because the board polls before the loop's first pass.
    """
    meta = meta or {}
    terminal_state = state.get("terminal") is True
    zone = zone_bounds(spec)
    zone_text = f"{format_price_value(zone[0])}-{format_price_value(zone[1])}" if zone else "-"
    status = "terminal" if terminal_state else ("active" if pollable else "idle")
    status_label = "FINISHED" if status == "terminal" else ("WATCHING" if status == "active" else "PAUSED")
    current_price = state.get("lastPrice") if isinstance(state.get("lastPrice"), (int, float)) else None
    target, trigger_text = watcher_trigger_target(spec)
    side = watcher_trigger_side(spec)
    progress = watcher_trigger_progress(spec, state, current_price, target, side)
    distance = distance_to_trigger(progress.get("basis"), target, zone, side)
    bar = requirement_bar(progress.get("percent", 0))
    last_checked = _parse_iso(state.get("lastChecked"))
    now = _parse_iso(now_iso) or _now()
    age_seconds = None if last_checked is None else max(0.0, (now - last_checked).total_seconds())
    icon, pulse = watcher_pulse_bar(
        frame=frame,
        running=running,
        pollable=pollable and not terminal_state,
        age_seconds=age_seconds,
        poll_seconds=poll_seconds,
    )
    market = _parse_iso(state.get("lastMarketTimestamp"))
    market_text = "-" if market is None else f"{market.strftime('%H:%M:%S')} UTC"
    last_tick = "-" if age_seconds is None else f"{format_duration(age_seconds)} ago"
    direction = str(spec.get("direction") or "").strip().upper() or "-"
    setup_phase = meta.get("setupPhase") or status.upper()
    quality = meta.get("rating") or meta.get("grade") or "not graded"
    candle_countdown = watcher_candle_countdown_line(spec, state, now_iso)
    workspace = f"\u00b7 {workspace_label} " if workspace_label else ""

    card_lines = [
        f"{icon} **{spec.get('pair') or '-'}** `{watcher_id}` {workspace}{direction} {status_label}",
        f"`{pulse}`",
        f"\U0001f4b9 `{format_price_value(current_price)}` \u2192 \U0001f3af `{target}` (`{distance}`) \u00b7 tick `#{state.get('pollCount') or 0}` \u00b7 last `{last_tick}` \u00b7 market `{market_text}`",
        f"\U0001f3af `{bar}` {progress.get('text')} \u00b7 {progress.get('need')}",
        f"{candle_countdown + chr(10) if candle_countdown else ''}\U0001f9ed {truncate(trigger_text, 120)}",
        f"\U0001f9f1 Zone `{zone_text}` \u00b7 \U0001f9ec `{setup_phase}` \u00b7 \u2b50 `{quality}`",
    ]
    card = "\n".join(card_lines).strip()

    invalidation_sentences = spec.get("invalidationConditions")
    if isinstance(invalidation_sentences, list) and invalidation_sentences:
        invalidation = [str(item) for item in invalidation_sentences]
    else:
        invalidation = [describe_rule(rule, spec.get("timeframe")) for rule in _invalidation_rules(spec)]
    stop_loss = meta.get("stopLoss")
    management = [describe_rule(rule, spec.get("timeframe")) for rule in _management_rules(spec)]
    plain_status = (
        "TRIGGER HIT \u2014 this watcher has finished."
        if terminal_state
        else "WATCHING \u2014 waiting for the trigger condition."
    )
    candle_tracking = watcher_candle_tracking_line(spec, state, target, side)
    recent = list(events or [])[-3:]

    detail_lines = [
        f"{icon} **{spec.get('pair') or '-'} Watcher** `{watcher_id}`" + (f" \u00b7 {workspace_label}" if workspace_label else ""),
        f"`{pulse}`",
        "",
        f"\U0001f4e1 **Status**: {plain_status}",
        f"\U0001f3af **Trigger progress**: `{bar}` {progress.get('text')} \u00b7 {progress.get('need')}",
        f"\U0001f4b9 **Current polled price**: `{format_price_value(current_price)}`",
        f"\U0001f3af **Trigger target**: `{target}` \u00b7 `{distance}`",
        f"\U0001f4cd **Price location**: `{zone_position(current_price, zone)}`",
        f"\U0001f9ed **Watching for**: {truncate(trigger_text, 260)}",
    ]
    if meta.get("nextStep"):
        detail_lines.append(f"\u27a1\ufe0f **Next after alert**: {truncate(meta['nextStep'], 260)}")
    detail_lines.extend([
        f"\U0001f9a0 **Watcher tick**: `#{state.get('pollCount') or 0}` \u00b7 last tick `{last_tick}`",
        "",
        f"\U0001f9f1 **Entry zone**: `{zone_text}`",
        f"\U0001f6d1 **Invalid if**: {truncate('; '.join(invalidation) or '-', 220)}",
        f"\U0001f3c1 **Stop / targets**: `{stop_loss if stop_loss is not None else '-'}` / `{_targets_text(meta.get('targets'))}`",
        f"\U0001f4c8 **Direction / TF**: `{direction}` / `{spec.get('timeframe') or '-'}`",
        f"\U0001f4e1 **Source**: `{spec.get('source') or 'yfinance'}`",
        f"\U0001f9ec **Setup phase**: `{setup_phase}`",
        f"\u2b50 **Quality**: `{quality}`",
    ])
    if management:
        detail_lines.append(f"\U0001f527 **Management**: {'; '.join(management)}")
    detail_lines.extend([
        "",
        f"\u2699\ufe0f **Poller**: `{'running' if running else 'stopped'}` \u00b7 every `{poll_seconds}s` \u00b7 `{state.get('pollCount') or 0}` ticks",
        f"\U0001f56f\ufe0f **Market data timestamp**: `{market_text}`",
    ])
    if candle_tracking:
        detail_lines.append(candle_tracking)
    if candle_countdown:
        detail_lines.append(candle_countdown)
    if state.get("lastError"):
        detail_lines.append(f"\U0001f4e6 Data feed ERROR x{state.get('errorCount') or 1}: {truncate(state['lastError'], 150)}")
    if recent:
        detail_lines.extend(["", "\U0001f552 **Recent events**"])
        for event in recent:
            detail_lines.append(
                f"- {event.get('eventType') or event.get('event_type') or ''} \u00b7 {_event_time_or_price(event)} \u00b7 {truncate(event.get('message'), 120)}"
            )

    return {
        "watcher_id": watcher_id,
        "pair": spec.get("pair") or "-",
        "terminal": terminal_state,
        "progress_percent": progress.get("percent", 0),
        "card": card,
        "detail": "\n".join(detail_lines).strip(),
    }


def watcher_item_to_board(
    item: Any, *, running: bool, poll_seconds: float, now_iso: Optional[str] = None
) -> Dict[str, Any]:
    """Adapt a :class:`~src.watcher.models.WatchItem` to the renderer's inputs.

    Keeps the board renderer decoupled from the watcher dataclass, so the same
    renderer serves any surface that can produce the structural shape.
    """
    state = item.watcher_state
    events = []
    for event in getattr(item, "_recent_events", []) or []:
        events.append(
            {
                "eventType": event.event_type,
                "message": event.message,
                "at": event.created_at,
                "price": event.price,
            }
        )
    spec = {
        "pair": item.pair,
        "assetClass": item.asset_class,
        "direction": item.direction,
        "timeframe": item.timeframe,
        "source": getattr(item, "source", "") or "",
        "rules": list(item.trigger_rules or []),
        "invalidationRules": list(item.invalidation_rules or []),
        "invalidationConditions": list(item.invalidation_conditions or []),
        "waitingTrigger": item.waiting_trigger,
        "expiry": item.expiry,
    }
    if item.entry_zone is not None:
        spec["entryZone"] = {"low": item.entry_zone.low, "high": item.entry_zone.high}
    context = item.analysis_context or {}
    meta = {
        "rating": context.get("rating"),
        "grade": context.get("grade"),
        "setupPhase": context.get("watcher_phase") or context.get("setup_status"),
        "nextStep": context.get("next_condition_after_trigger") or context.get("next_condition"),
        "stopLoss": context.get("stop_loss"),
        "targets": context.get("targets"),
    }
    state_dict = {
        "armedAt": state.armed_at,
        "lastChecked": state.last_checked,
        "lastPrice": state.last_price,
        "lastMarketTimestamp": state.last_market_timestamp,
        "lastConfirmedCandle": state.last_confirmed_candle,
        "previousConfirmedCandle": state.previous_confirmed_candle,
        "terminal": state.terminal,
        "pollCount": state.poll_count,
        "errorCount": state.error_count,
        "lastError": state.last_error,
        "firedConditions": state.fired_conditions,
    }
    return {
        "watcher_id": item.id,
        "spec": spec,
        "state": state_dict,
        "meta": meta,
        "events": events,
        "pollable": item.is_pollable,
        "poll_seconds": poll_seconds,
        "running": running,
        "now_iso": now_iso or _iso(_now()),
    }


# ==================== Telegram pager chrome ====================

#: Telegram's text limit. Kept below it so an edit never fails the send.
TELEGRAM_BOARD_TEXT_MAX = 4000

#: Callback-data prefix. Deliberately tiny: Telegram caps callback data at 64
#: bytes, and there is exactly one board message per chat, so the message id
#: already identifies the session and watcher ids never travel in the payload.
TELEGRAM_WATCH_PREFIX = "wb:"
CONTROL_NEXT = f"{TELEGRAM_WATCH_PREFIX}n"
CONTROL_PREV = f"{TELEGRAM_WATCH_PREFIX}p"
CONTROL_TOGGLE = f"{TELEGRAM_WATCH_PREFIX}t"
CONTROL_DELETE = f"{TELEGRAM_WATCH_PREFIX}d"
CONTROL_CLOSE = f"{TELEGRAM_WATCH_PREFIX}x"


def escape_telegram_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_telegram_board_html(markdown: str) -> str:
    """Convert the board renderer's small Markdown subset to Telegram HTML.

    The renderer only emits ``**bold**`` and `` `code` `` plus plain prose, so
    this is a total function over that subset rather than a general parser.
    Escaping happens FIRST so a ``<`` in watcher content cannot inject a tag.
    """
    escaped = escape_telegram_html(markdown)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", "\u003cb\u003e\\1\u003c/b\u003e", escaped)
    escaped = re.sub(r"`([^`\n]+)`", "\u003ccode\u003e\\1\u003c/code\u003e", escaped)
    return escaped


def clamp_board_index(index: int, count: int) -> int:
    """Clamp a pager index into range. An empty board reports 0."""
    if count <= 0:
        return 0
    try:
        value = int(index)
    except (TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    if value > count - 1:
        return count - 1
    return value


def parse_telegram_watch_control(data: str) -> Optional[str]:
    """Parse a callback payload into a control name, or None when not ours.

    Returning None lets the caller fall through to the generic handler instead
    of swallowing the press.
    """
    mapping = {
        CONTROL_NEXT: "next",
        CONTROL_PREV: "prev",
        CONTROL_TOGGLE: "toggle",
        CONTROL_DELETE: "delete",
        CONTROL_CLOSE: "close",
    }
    return mapping.get(str(data or ""))


def format_telegram_watcher_board(
    *,
    items: Sequence[Any],
    index: int,
    expanded: Any,
    frame: int,
    now_iso: str,
    refresh_seconds: int,
    running: bool,
    poll_seconds: float,
) -> Dict[str, Any]:
    """Build the whole board message: header, current card (or card + detail),
    and the button rows.

    ``items`` is a sequence of rendered-card dicts (from
    :func:`render_watcher_board_card`), each also carrying ``watcher_id`` so the
    pager can key the expanded set. Pure and total: an empty board renders an
    explicit ``no watchers armed`` state with only a Close button, rather than a
    half-drawn pager with dead arrows.
    """
    items = list(items or [])
    if not items:
        return {
            "html": (
                "\U0001f441 \u003cb\u003eWatcher Board\u003c/b\u003e \u00b7 no watchers armed\n\n"
                "Arm a watcher in the agent and it appears here on the next tick."
            ),
            "actions": [[{"text": "\u2716 Close", "data": CONTROL_CLOSE}]],
            "index": 0,
        }

    index = clamp_board_index(index, len(items))
    item = items[index]
    watcher_id = str(item.get("watcher_id") or "")
    expanded_set = expanded or set()
    is_expanded = watcher_id in expanded_set
    clock = now_iso[11:19] if len(now_iso) >= 19 else ""

    header = (
        f"\U0001f441 \u003cb\u003eWatcher Board\u003c/b\u003e \u00b7 {index + 1} of {len(items)}"
        + (f" \u00b7 {clock} UTC" if clock else "")
        + f" \u00b7 refresh {refresh_seconds}s"
    )
    body = item.get("detail") if is_expanded else item.get("card")
    html = "\n".join([header, "", to_telegram_board_html(str(body or ""))])
    if len(html) > TELEGRAM_BOARD_TEXT_MAX:
        html = html[: TELEGRAM_BOARD_TEXT_MAX - 1].rstrip() + "\u2026"

    pager: List[Dict[str, str]] = []
    if index > 0:
        pager.append({"text": "\u25c0 Previous", "data": CONTROL_PREV})
    if index < len(items) - 1:
        pager.append({"text": "Next \u25b6", "data": CONTROL_NEXT})

    actions: List[List[Dict[str, str]]] = [
        [
            {
                "text": "\U0001f53c Collapse" if is_expanded else "\U0001f50e Details",
                "data": CONTROL_TOGGLE,
            },
            {"text": "\U0001f5d1 Delete", "data": CONTROL_DELETE},
            {"text": "\u2716 Close", "data": CONTROL_CLOSE},
        ]
    ]
    if pager:
        actions.append(pager)

    return {"html": html, "actions": actions, "index": index}

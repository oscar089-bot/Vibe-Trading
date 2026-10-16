"""Structured watcher ingestion from assistant analysis output.

Ported verbatim from the Vibe-Trading (v0.1.6) watcher subsystem. The model
can arm a watcher by emitting a fenced ``WATCHER-JSON`` block in its reply;
this module extracts those payloads and converts them into
:class:`~src.watcher.models.WatchItem` records.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Optional

from src.watcher.models import PriceZone, WatchItem, WatchKind, WatchStatus

_FENCE_RE = re.compile(
    r"```(?P<label>[^\n`]*)\n?(?P<body>[\s\S]*?)```",
    flags=re.IGNORECASE,
)
_WATCHER_MARKER_RE = re.compile(r"^\s*WATCHER[-_ ]JSON\s*", flags=re.IGNORECASE)
_INLINE_WATCHER_MARKER_RE = re.compile(r"WATCHER[-_ ]JSON", flags=re.IGNORECASE)


def _iter_json_payloads(text: str) -> Iterable[Any]:
    seen: set[str] = set()
    decoder = json.JSONDecoder()
    for match in _FENCE_RE.finditer(text or ""):
        label = match.group("label").strip().lower()
        body = _strip_watcher_marker(match.group("body").strip())
        if label and label not in {"watcher-json", "watcher_json", "json"} and not _WATCHER_MARKER_RE.match(label):
            continue
        if not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        key = json.dumps(payload, sort_keys=True, default=str)
        seen.add(key)
        yield payload

    for match in _INLINE_WATCHER_MARKER_RE.finditer(text or ""):
        start = (text or "").find("{", match.end())
        if start < 0:
            continue
        try:
            payload, _end = decoder.raw_decode((text or "")[start:])
        except json.JSONDecodeError:
            continue
        key = json.dumps(payload, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        yield payload


def _iter_candidates(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("watchers"), list):
        for item in payload["watchers"]:
            if isinstance(item, dict):
                yield item
        return
    yield payload


def _waiting_status(value: Any) -> bool:
    normalized = _normalize_status_text(value)
    if normalized in {"", "ACTIVE", "WAITING", "WAITING - ACTIVE", "WAITING - AGGRESSIVE"}:
        return True
    return normalized.startswith(("WAITING ", "WAITING-", "WAITING - "))


def _normalize_direction(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"BUY", "BULLISH"}:
        return "LONG"
    if normalized in {"SELL", "BEARISH"}:
        return "SHORT"
    return normalized


def _strip_watcher_marker(value: str) -> str:
    return _WATCHER_MARKER_RE.sub("", value or "").strip()


def _normalize_status_text(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    normalized = normalized.replace("—", "-").replace("–", "-")
    normalized = re.sub(r"\s*-\s*", " - ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _infer_asset_class(pair: str, value: Any) -> str:
    explicit = str(value or "").strip().lower()
    if explicit:
        return explicit
    normalized = re.sub(r"[^A-Z]", "", (pair or "").upper().replace("=X", ""))
    crypto_quotes = ("USDT", "USDC", "BTC", "ETH")
    if len(normalized) == 6 and not normalized.endswith(crypto_quotes):
        return "forex"
    return "crypto"


def _entry_zone_from_payload(payload: dict[str, Any]) -> Optional[PriceZone]:
    raw = payload.get("entry_zone")
    zone = PriceZone.from_dict(raw)
    if zone is not None:
        return zone
    if isinstance(raw, dict):
        for key in ("primary", "aggressive"):
            zone = PriceZone.from_dict(raw.get(key))
            if zone is not None:
                return zone
    return None


def _infer_timeframe(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("timeframe") or "").strip()
    if explicit:
        return explicit
    candidates: list[str] = []
    raw_zone = payload.get("entry_zone")
    if isinstance(raw_zone, dict):
        for key in ("primary", "aggressive"):
            zone = raw_zone.get(key)
            if isinstance(zone, dict):
                candidates.append(str(zone.get("type") or ""))
    candidates.extend(
        str(payload.get(key) or "")
        for key in ("waiting_trigger", "activation_trigger", "invalidation_trigger")
    )
    for candidate in candidates:
        match = re.search(r"\b(\d+)\s*([MHDW])\b", candidate, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)}{match.group(2).upper()}"
    return ""


def _alternate_entry_zones(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("entry_zone")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, dict) and PriceZone.from_dict(value) is not None
    }


def _invalidation_conditions(payload: dict[str, Any]) -> list[str]:
    conditions = list(payload.get("invalidation_conditions") or [])
    trigger = str(payload.get("invalidation_trigger") or "").strip()
    if trigger:
        conditions.append(trigger)
    return conditions


def _invalidation_rules(payload: dict[str, Any], direction: str) -> list[dict[str, Any]]:
    rules = list(payload.get("invalidation_rules") or [])
    if rules or payload.get("stop_loss") is None:
        return rules
    side = "below" if direction == "LONG" else "above" if direction == "SHORT" else ""
    if not side:
        return rules
    try:
        level = float(payload["stop_loss"])
    except (TypeError, ValueError):
        return rules
    rules.append({"type": "price_breaks_level", "side": side, "level": level})
    return rules


def _trigger_rule_from_text(text: Any, *, timeframe: str = "", source: str = "") -> Optional[dict[str, Any]]:
    raw = str(text or "")
    if not raw:
        return None
    close_match = re.search(
        r"\b(?:close|closes|closing|candle close)\b.{0,80}?(?:\u003e|above|over)\s*([0-9]+(?:\.[0-9]+)?)",
        raw,
        flags=re.IGNORECASE,
    )
    if not close_match:
        return None
    rule: dict[str, Any] = {
        "type": "price_breaks_level",
        "side": "above",
        "level": float(close_match.group(1)),
    }
    if timeframe:
        rule["timeframe"] = timeframe
    if source:
        rule["source"] = source
    return rule


def _infer_trigger_rules(payload: dict[str, Any], entry_zone: Optional[PriceZone]) -> list[dict[str, Any]]:
    rules = list(payload.get("trigger_rules") or [])
    if rules:
        return rules

    entry_trigger = payload.get("entry_trigger")
    if isinstance(entry_trigger, dict):
        rule = _trigger_rule_from_text(
            entry_trigger.get("condition"),
            timeframe=str(entry_trigger.get("tf") or "").strip(),
            source="entry_trigger",
        )
        if rule is not None:
            return [rule]

    for index, text in enumerate(payload.get("monitoring_rules") or []):
        rule = _trigger_rule_from_text(text, source=f"monitoring_rules[{index}]")
        if rule is not None:
            return [rule]

    activation_rule = _trigger_rule_from_text(payload.get("activation_trigger"), source="activation_trigger")
    if activation_rule is not None:
        return [activation_rule]

    if entry_zone is not None:
        return [
            {
                "type": "price_enters_zone",
                "low": entry_zone.low,
                "high": entry_zone.high,
            }
        ]
    return []


def watch_item_from_payload(
    payload: dict[str, Any],
    *,
    source_session_id: Optional[str] = None,
    source_attempt_id: Optional[str] = None,
) -> Optional[WatchItem]:
    """Convert a structured watcher JSON object into a WatchItem."""
    kind = str(payload.get("kind") or "waiting_setup").strip()
    setup_status = payload.get("setup_status", payload.get("status"))
    if kind != WatchKind.WAITING_SETUP.value or not _waiting_status(setup_status):
        return None
    pair = str(payload.get("pair") or "").strip()
    if not pair:
        return None

    direction = _normalize_direction(payload.get("direction"))
    entry_zone = _entry_zone_from_payload(payload)
    trigger_rules = _infer_trigger_rules(payload, entry_zone)

    context = dict(payload.get("analysis_context") or {})
    if setup_status:
        context.setdefault("setup_status", _normalize_status_text(setup_status))
    alternate_zones = _alternate_entry_zones(payload)
    if alternate_zones:
        context.setdefault("entry_zones", alternate_zones)
    for key in (
        "rating",
        "grade",
        "confluence_score",
        "confluences",
        "llm_summary",
        "original_analysis_excerpt",
        "activation_trigger",
        "entry_trigger",
        "reason_for_upgrade",
        "reason_for_status",
        "downgrade_trigger",
        "monitoring_rules",
        "partial_profit_rule",
        "stop_loss",
        "targets",
        "timestamp",
        "previous_watcher",
    ):
        if key in payload and key not in context:
            context[key] = payload[key]
    entry_trigger = payload.get("entry_trigger")
    entry_trigger_condition = ""
    if isinstance(entry_trigger, dict):
        entry_trigger_condition = str(entry_trigger.get("condition") or "").strip()

    return WatchItem(
        status=WatchStatus.ACTIVE,
        kind=WatchKind.WAITING_SETUP,
        pair=pair,
        asset_class=_infer_asset_class(pair, payload.get("asset_class")),
        direction=direction,
        timeframe=_infer_timeframe(payload),
        entry_zone=entry_zone,
        waiting_trigger=str(payload.get("waiting_trigger") or payload.get("activation_trigger") or entry_trigger_condition or "").strip(),
        trigger_rules=trigger_rules,
        invalidation_conditions=_invalidation_conditions(payload),
        invalidation_rules=_invalidation_rules(payload, direction),
        expiry=payload.get("expiry"),
        session_window=payload.get("session_window"),
        source_session_id=payload.get("source_session_id") or source_session_id,
        source_attempt_id=payload.get("source_attempt_id") or source_attempt_id,
        analysis_context=context,
    )


def extract_watch_items(
    text: str,
    *,
    source_session_id: Optional[str] = None,
    source_attempt_id: Optional[str] = None,
) -> List[WatchItem]:
    """Extract structured watcher items from assistant output."""
    items: list[WatchItem] = []
    for payload in _iter_json_payloads(text):
        for candidate in _iter_candidates(payload):
            item = watch_item_from_payload(
                candidate,
                source_session_id=source_session_id,
                source_attempt_id=source_attempt_id,
            )
            if item is not None:
                items.append(item)
    return items

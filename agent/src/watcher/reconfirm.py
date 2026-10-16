"""One-shot LLM reconfirmation helpers for watcher events.

Ported from the Vibe-Trading (v0.1.6) watcher subsystem.

DISABLED BY DEFAULT. The wake path already sends the fired watcher's plan and
live event to the owning session, where the model re-analyses and re-arms — so
the reconfirmation pass is a *second* LLM call that duplicates work the wake
path does. It ships as an opt-in (``VIBE_TRADING_WATCHER_LLM_CONFIRMATION``,
default false) for operators who want a cheap in-loop pre-filter before the
session wakes. Nothing here is wired on by default.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from src.watcher.ingest import watch_item_from_payload
from src.watcher.models import WatchItem, WatchStatus, WatcherEvent

_JSON_FENCE_RE = re.compile(r"```(?:watcher-update-json|json)\s*(?P<body>[\s\S]*?)```", flags=re.IGNORECASE)


def build_reconfirmation_prompt(item: WatchItem, event: WatcherEvent) -> str:
    """Build the full-context watcher reconfirmation prompt."""
    context = item.analysis_context or {}
    zone = item.entry_zone.to_dict() if item.entry_zone else None
    invalidation = item.invalidation_conditions or [str(rule) for rule in item.invalidation_rules]
    lines = [
        f"The watcher condition fired for {item.pair}.",
        "",
        "Original setup:",
        f"- Direction: {item.direction or '-'}",
        f"- Rating: {context.get('rating', '-')} / {context.get('grade', '-')}",
        f"- Confluences: {', '.join(str(v) for v in context.get('confluences', []) or []) or '-'}",
        f"- Entry zone: {zone or '-'}",
        f"- Invalidation: {'; '.join(str(v) for v in invalidation) or '-'}",
        f"- Waiting trigger: {item.waiting_trigger or '-'}",
        f"- Original summary: {context.get('llm_summary') or context.get('original_analysis_excerpt') or '-'}",
        "",
        "Current watcher event:",
        f"- Event type: {event.event_type}",
        f"- Reason: {event.message or '-'}",
        f"- Current price: {event.price if event.price is not None else item.watcher_state.last_price}",
        f"- Last checked: {item.watcher_state.last_checked or event.created_at}",
        "",
        "Task:",
        "Reconfirm, update, or cancel the setup. Do not place orders or imply execution.",
        "Return one fenced watcher-update-json block only, with this shape:",
        "```watcher-update-json",
        "{",
        '  "outcome": "CONFIRMED_VALID | STILL_WAITING_UPDATED | INVALIDATED | NO_TRADE",',
        '  "reason": "short trader-readable explanation",',
        '  "watcher": null',
        "}",
        "```",
        "If outcome is STILL_WAITING_UPDATED, watcher must contain a full waiting_setup watcher JSON object.",
    ]
    return "\n".join(lines)


def parse_reconfirmation_response(text: str) -> Optional[dict[str, Any]]:
    """Parse a structured watcher reconfirmation response."""
    for match in _JSON_FENCE_RE.finditer(text or ""):
        try:
            payload = json.loads(match.group("body").strip())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("outcome"):
            return payload
    try:
        payload = json.loads((text or "").strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) and payload.get("outcome") else None


def apply_reconfirmation_update(item: WatchItem, payload: dict[str, Any]) -> tuple[WatchItem, str]:
    """Apply a parsed reconfirmation payload to an existing watcher."""
    outcome = str(payload.get("outcome") or "").strip().upper()
    reason = str(payload.get("reason") or "").strip()
    if outcome == "STILL_WAITING_UPDATED":
        watcher_payload = payload.get("watcher")
        if isinstance(watcher_payload, dict):
            updated = watch_item_from_payload(
                watcher_payload,
                source_session_id=item.source_session_id,
                source_attempt_id=item.source_attempt_id,
            )
            if updated is not None:
                updated.id = item.id
                updated.watcher_state = item.watcher_state
                updated.watcher_state.llm_reconfirmed = True
                return updated, reason
        item.watcher_state.llm_reconfirmed = True
        return item, reason
    if outcome in {"INVALIDATED", "NO_TRADE"}:
        item.status = WatchStatus.INVALIDATED if outcome == "INVALIDATED" else WatchStatus.CANCELLED
        item.watcher_state.llm_reconfirmed = True
        item.watcher_state.last_event = outcome
        return item, reason
    if outcome == "CONFIRMED_VALID":
        item.status = WatchStatus.TRIGGERED
        item.watcher_state.llm_reconfirmed = True
        item.watcher_state.last_event = outcome
        return item, reason
    item.watcher_state.llm_reconfirmed = True
    return item, reason

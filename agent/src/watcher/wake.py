"""Fire-path helpers for a fired watcher.

Two responsibilities, both ported from the OpenAlice watcher port's
``wake-prompt.ts``:

* :func:`build_wake_prompt` — the prompt handed to the woken session. It
  mirrors the fork's ``build_reconfirmation_prompt`` structure (original setup,
  then what just happened, then an explicit task), which is what lets the woken
  model know which phase it is in and what to do next.
* :func:`build_alert` — a short MACHINE-generated summary for the IM channel.
  Assembled from the evaluator's own event list, never written by a model, so
  the operator learns a condition fired even if the woken session never replies.

:class:`WatcherDispatcher` wires those to the session runtime and the channel
manager. Both collaborators are injected so the fire path is testable without a
live session runtime or a network — the same discipline the scheduled-research
executor uses for its dispatcher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.watcher.evaluator import MarketSnapshot
from src.watcher.models import WatchItem, WatcherEvent

logger = logging.getLogger(__name__)


#: Human label per event type, for the wake prompt and the alert.
_EVENT_LABELS = {
    "TRIGGER_HIT": "Trigger met",
    "TRIGGER_CLOSE": "Candle close trigger met",
    "MANAGEMENT_ALERT": "Trade-management alert",
    "INVALIDATED": "Setup invalidated",
    "EXPIRED": "Watcher expired",
    "ERROR": "Watcher data error",
}


def _fmt(value: Optional[float]) -> str:
    """Format a price for display, or ``-`` when absent."""
    if value is None:
        return "-"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "-"


def _describe(event: WatcherEvent) -> str:
    return _EVENT_LABELS.get(event.event_type, event.event_type)


def _event_lines(events: list[WatcherEvent], snapshot: MarketSnapshot) -> list[str]:
    lines: list[str] = []
    for event in events:
        price = event.price if event.price is not None else snapshot.price
        lines.append(f"- {_describe(event)}: {event.message} (price {_fmt(price)})")
    return lines


def _setup_lines(watcher: WatchItem) -> list[str]:
    """The original setup context the model authored when it armed the watcher."""
    context = watcher.analysis_context or {}
    lines = [
        f"- Pair: {watcher.pair} ({watcher.asset_class}"
        + (f", {watcher.timeframe}" if watcher.timeframe else "")
        + ")",
        f"- Direction: {watcher.direction or '-'}",
    ]
    rating = context.get("rating") or context.get("grade")
    if rating:
        lines.append(f"- Rating: {rating}")
    zone = watcher.entry_zone.to_dict() if watcher.entry_zone else None
    if zone:
        lines.append(f"- Entry zone: {zone['low']}-{zone['high']}")
    if watcher.waiting_trigger:
        lines.append(f"- Waiting trigger: {watcher.waiting_trigger}")
    invalidation = watcher.invalidation_conditions or [
        str(rule) for rule in watcher.invalidation_rules
    ]
    if invalidation:
        lines.append(f"- Invalidation: {'; '.join(str(v) for v in invalidation)}")
    summary = context.get("llm_summary") or context.get("original_analysis_excerpt")
    if summary:
        lines.append(f"- Original summary: {summary}")
    return lines


def build_wake_prompt(
    watcher: WatchItem,
    events: list[WatcherEvent],
    snapshot: MarketSnapshot,
    *,
    terminal: bool,
) -> str:
    """Build the prompt handed to the woken session.

    Structure:
      1. The original setup the model authored when arming the watcher.
      2. What just happened — the fired rule(s), live price, observation time.
      3. The task — re-analyse and re-arm, following the setup above.
    """
    lines: list[str] = [
        f"A market watcher fired on {watcher.pair} (watcher {watcher.id}).",
        "",
        "## Original setup",
        "",
        *_setup_lines(watcher),
        "",
        "## What happened",
        "",
        f"- Observed price: {_fmt(snapshot.price)}",
        f"- Observed at: {snapshot.timestamp}",
        *_event_lines(events, snapshot),
        "",
        "## Task",
        "",
    ]
    if terminal:
        lines.append("This watcher has reached a terminal state and will not fire again.")
    lines.extend(
        [
            "Re-analyse the setup against the event above and report your conclusion to the user.",
            "If the thesis still applies, update this watcher with the next deterministic condition",
            "so it can be re-armed for the next phase. Otherwise cancel the watcher.",
            "Do not place orders or imply execution.",
        ]
    )
    return "\n".join(lines)


def build_alert(
    watcher: WatchItem,
    events: list[WatcherEvent],
    snapshot: MarketSnapshot,
) -> str:
    """Build the short machine-facing alert for the IM channel.

    Never the model prompt: this is assembled from the evaluator's own event
    list so the operator learns a condition fired independently of the woken
    session's reply.
    """
    lines = [
        f"Watcher fired — {watcher.pair} ({watcher.id})",
        f"{watcher.pair} at {_fmt(snapshot.price)}",
    ]
    for event in events:
        lines.append(f"- {_describe(event)}: {event.message}")
    return "\n".join(lines)


#: Wake the owning session with a prompt. Returns True when the wake was
#: accepted (queued), False when it could not be delivered.
WakeCallback = Callable[[str, str], Awaitable[bool]]
#: Send a machine alert to the configured IM channel.
AlertCallback = Callable[[str], Awaitable[None]]


@dataclass
class WatcherDispatcher:
    """Fire-path dispatcher with injected collaborators.

    The wake and the alert are independent: a failed alert never blocks the
    wake, and a failed wake never blocks the alert. Each failure is logged and
    swallowed so the poll loop is never stopped by a delivery problem.

    Attributes:
        wake: ``async (session_id, prompt) -> bool``. Wakes the owning session.
            ``None`` disables the wake path.
        alert: ``async (text) -> None``. Sends the machine alert. ``None``
            disables the alert path.
    """

    wake: Optional[WakeCallback] = None
    alert: Optional[AlertCallback] = None

    async def dispatch(self, fire) -> None:
        """Deliver one fire: alert first (fast, best-effort), then the wake."""
        watcher: WatchItem = fire.watcher
        events: list[WatcherEvent] = fire.events
        snapshot: MarketSnapshot = fire.snapshot

        if self.alert is not None:
            try:
                await self.alert(build_alert(watcher, events, snapshot))
            except Exception:
                logger.warning("watcher alert delivery failed for %s", watcher.id, exc_info=True)

        session_id = (watcher.session_id or watcher.source_session_id or "").strip()
        if self.wake is None or not session_id:
            return
        prompt = build_wake_prompt(watcher, events, snapshot, terminal=fire.terminal)
        try:
            await self.wake(session_id, prompt)
        except Exception:
            logger.warning("watcher wake failed for %s (session %s)", watcher.id, session_id, exc_info=True)

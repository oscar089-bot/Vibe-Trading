"""Forward attempt status to IM adapters without releasing unchecked answers."""
from __future__ import annotations

import time
from typing import Any

from src.channels.activity import Activity, render_activity, render_activity_rich
from src.channels.bus.events import InboundMessage, OutboundMessage

# Minimum gap between two spinner-only re-renders. Telegram allows roughly one
# edit per second per chat, so ~0.8s is the practical floor for a spinner that
# reads as continuous motion rather than a once-a-second twitch.
_ACTIVITY_ANIMATE_INTERVAL_S = 0.8


class AttemptProgress:
    """Poll buffered session events with an attempt-scoped replay cursor."""

    def __init__(self, bus: Any, event_bus: Any, msg: InboundMessage,
                 session_id: str, attempt_id: str) -> None:
        self.bus = bus
        self.events = event_bus
        self.msg = msg
        self.session_id = session_id
        self.attempt_id = attempt_id
        self.cursor: str | None = None
        self.streaming = bool(msg.metadata.get("_wants_stream"))
        self.stream_started = False
        self.reasoning_started = False
        self.reasoning_chars: dict[int, int] = {}
        # The single editable status line a channel renders while the turn
        # runs. Sent once and edited in place, then removed on completion, so
        # it never becomes a leftover "Working…" message above the answer.
        self.activity_started = False
        self.activity = Activity()
        self._activity_frame = 0
        self._activity_rendered = ""
        self._activity_rendered_at = 0.0

    async def _send(self, content: str, **flags: Any) -> None:
        await self.bus.publish_outbound(OutboundMessage(
            channel=self.msg.channel, chat_id=self.msg.chat_id, content=content,
            metadata={**self.msg.metadata, "_channel_runtime": True,
                      "session_id": self.session_id, "attempt_id": self.attempt_id,
                      "_stream_id": self.attempt_id, **flags},
        ))

    async def poll(self) -> None:
        """Forward status and opt-in reasoning, excluding answer drafts/results."""
        for event in self.events.replay(self.session_id, self.cursor, replay_all=True):
            self.cursor = event.event_id
            data = event.data
            if data.get("attempt_id") != self.attempt_id:
                continue
            kind = event.event_type
            if kind == "reasoning_delta":
                iteration = int(data.get("iter", 0))
                chars = int(data.get("chars", 0))
                seen = self.reasoning_chars.get(iteration, 0)
                self.reasoning_chars[iteration] = max(seen, chars)
                # AgentLoop emits a bounded cumulative tail, not a delta.
                # A missing tail is deliberate grounding redaction: show only
                # a status, never reconstruct text from any other payload.
                tail = data.get("tail")
                if isinstance(tail, str) and chars > seen and tail:
                    self.reasoning_started = True
                    await self._send(tail[-min(chars - seen, len(tail)):], _reasoning_delta=True)
                    continue
                if tail is not None or chars <= seen:
                    continue
            if kind in {"tool_call", "grounding_status", "attempt.completed", "attempt.failed"}:
                await self._end_reasoning()
            tool = str(data.get("tool") or "")
            if kind == "tool_call" and tool:
                self.activity.note_call(tool)
            elif kind == "tool_result" and tool:
                self.activity.note_result(
                    tool,
                    ok=str(data.get("status") or "ok") == "ok",
                    duration_ms=data.get("elapsed_ms"),
                    error=str(data.get("error") or ""),
                )

            status = {
                "attempt.created": "Working on your request…",
                "reasoning_delta": "Reasoning…",
                "llm_waiting": "Waiting for the model…",
                "tool_call": f"Using {tool}…" if tool else "Working…",
                "tool_heartbeat": f"Still running {tool}…" if tool else "Still working…",
                "grounding_status": "Checking the figures…",
            }.get(kind)
            # Arbitrary progress payloads, tool results and text_delta may
            # contain unvalidated figures. Only these status labels go out.
            if status:
                self.activity.narration = status
                await self._render_activity()
            elif kind == "tool_result":
                # A finished tool changes the counts/failure lines even without
                # a new status label, so the panel is re-rendered on the result.
                await self._render_activity()

        await self._animate_activity()

    async def _render_activity(self, *, force: bool = False) -> None:
        """Render and send the activity panel, skipping an unchanged frame."""
        plain = render_activity(self.activity, self._activity_frame)
        if not force and plain == self._activity_rendered:
            return
        self._activity_frame += 1
        self._activity_rendered = plain
        self._activity_rendered_at = time.monotonic()
        self.activity_started = True
        # ``_stream_delta`` is still set so channels that build a streaming
        # card from the same signal (Feishu) keep working unchanged; the
        # ``_activity`` flag lets a channel render ONE line it edits in place.
        flags: dict[str, Any] = {
            "_progress": True,
            "_tool_hint": self.activity.current_tool is not None,
            "_activity": True,
            "_activity_rich": render_activity_rich(self.activity, self._activity_frame),
        }
        if self.streaming:
            flags["_stream_delta"] = True
            self.stream_started = True
        await self._send(plain + "\n", **flags)

    async def _animate_activity(self) -> None:
        """Advance the spinner while a tool runs, so the line looks alive."""
        if not self.activity_started or self.activity.current_tool is None:
            return
        if (time.monotonic() - self._activity_rendered_at) < _ACTIVITY_ANIMATE_INTERVAL_S:
            return
        await self._render_activity(force=True)

    async def _end_reasoning(self) -> None:
        if self.reasoning_started:
            await self._send("", _reasoning_end=True)
            self.reasoning_started = False

    async def close(self) -> None:
        """Close transient cards on completion, timeout or cancellation."""
        await self._end_reasoning()
        if self.activity_started:
            # Clear the live status line so the final answer stands alone.
            # Channels that do not override ``send_activity`` ignore this.
            await self._send("", _activity=True, _activity_end=True)
            self.activity_started = False
        if self.stream_started:
            await self._send("", _stream_end=True, _resuming=True)
            self.stream_started = False

"""Telegram live activity line: created, edited in place, then deleted."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any


def _stub_telegram_deps() -> None:
    """Install minimal stubs so telegram.py imports without python-telegram-bot."""

    def _mod(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    tg = _mod("telegram")

    class _Accepting:
        def __init__(self, *args, **kwargs) -> None:
            pass

    for name in (
        "BotCommand",
        "InlineKeyboardButton",
        "InlineKeyboardMarkup",
        "ReactionTypeEmoji",
        "ReplyParameters",
        "Update",
    ):
        setattr(tg, name, _Accepting)

    err = _mod("telegram.error")
    for name in ("BadRequest", "NetworkError", "TimedOut", "RetryAfter"):
        setattr(err, name, type(name, (Exception,), {}))
    # RetryAfter carries a retry_after attribute in the real SDK.
    err.RetryAfter = type("RetryAfter", (Exception,), {"retry_after": 0})

    ext = _mod("telegram.ext")
    for name in (
        "Application",
        "CallbackQueryHandler",
        "ContextTypes",
        "MessageHandler",
        "filters",
    ):
        setattr(ext, name, _Accepting)

    _mod("telegram.request").HTTPXRequest = _Accepting


_stub_telegram_deps()

from src.channels.bus.queue import MessageBus  # noqa: E402
from src.channels.telegram import TelegramChannel, TelegramConfig  # noqa: E402


class _SentMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeBot:
    """Records send/edit/delete calls so the activity lifecycle is observable."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edited: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self._next_id = 100

    async def send_message(self, *, chat_id: int, text: str, **_: Any) -> _SentMessage:
        self.sent.append(text)
        self._next_id += 1
        return _SentMessage(self._next_id)

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, **_: Any) -> None:
        self.edited.append((message_id, text))

    async def delete_message(self, *, chat_id: int, message_id: int, **_: Any) -> None:
        self.deleted.append(message_id)


class _FakeApp:
    def __init__(self) -> None:
        self.bot = _FakeBot()


def _channel() -> tuple[TelegramChannel, _FakeBot]:
    channel = TelegramChannel(TelegramConfig(token="x"), MessageBus())
    app = _FakeApp()
    channel._app = app  # type: ignore[assignment]

    async def _passthrough(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await fn(*args, **kwargs)

    # Bypass the real retry wrapper: it imports ``telegram.error.RetryAfter``,
    # which the shared telegram stub in sibling test files does not define. The
    # activity path itself is what is under test, not the retry policy.
    channel._call_with_retry = _passthrough  # type: ignore[assignment]
    return channel, app.bot


def test_activity_line_is_created_then_edited_in_place() -> None:
    """One message is created, later statuses edit it, no second message appears."""

    async def scenario() -> None:
        channel, bot = _channel()
        await channel.send_activity("1", "Working on your request…\n", {"_activity": True})
        await channel.send_activity("1", "Using get_market_data…\n", {"_activity": True})
        await channel.send_activity("1", "Checking the figures…\n", {"_activity": True})

        assert bot.sent == ["Working on your request…"]
        assert len(bot.edited) == 2
        message_id = channel._activity_msgs["1"]
        assert all(mid == message_id for mid, _ in bot.edited)
        assert bot.edited[-1][1] == "Checking the figures…"

    asyncio.run(scenario())


def test_activity_line_is_deleted_on_end() -> None:
    """The activity-end signal removes the line so the answer stands alone."""

    async def scenario() -> None:
        channel, bot = _channel()
        await channel.send_activity("1", "Working on your request…\n", {"_activity": True})
        message_id = channel._activity_msgs["1"]
        await channel.send_activity("1", "", {"_activity": True, "_activity_end": True})

        assert bot.deleted == [message_id]
        assert channel._activity_msgs == {}

    asyncio.run(scenario())


def test_repeated_identical_status_does_not_edit() -> None:
    """An unchanged status must not spend an edit (Telegram is rate-limited)."""

    async def scenario() -> None:
        channel, bot = _channel()
        await channel.send_activity("1", "Working on your request…\n", {"_activity": True})
        await channel.send_activity("1", "Working on your request…\n", {"_activity": True})

        assert bot.sent == ["Working on your request…"]
        assert bot.edited == []

    asyncio.run(scenario())


def test_activity_end_without_a_line_is_a_noop() -> None:
    """Clearing when nothing was shown must not raise or delete anything."""

    async def scenario() -> None:
        channel, bot = _channel()
        await channel.send_activity("1", "", {"_activity": True, "_activity_end": True})

        assert bot.sent == []
        assert bot.deleted == []

    asyncio.run(scenario())


def test_failure_lines_clear_on_the_next_success() -> None:
    """A success collapses stale failure rows to the header count.

    Reproduces the reported bug: seven failed "Fetching market data" calls
    left seven `⚠️` lines above the answer for the rest of the turn. The count
    stays in the header (``13 actions · 8 failed``); the detail lines are
    transient and must clear once the turn moves on.
    """
    from src.channels.activity import Activity, render_activity

    activity = Activity()
    for _ in range(7):
        activity.note_call("get_market_data")
        activity.note_result("get_market_data", ok=False, duration_ms=40)

    rendered = render_activity(activity, 0)
    assert rendered.count("⚠️") == 7
    assert "7 failed" in rendered

    # One success -> failure rows collapse, count remains.
    activity.note_call("search_symbol")
    activity.note_result("search_symbol", ok=True, duration_ms=120)

    rendered = render_activity(activity, 0)
    assert "⚠️" not in rendered, "stale failure rows must clear on success"
    assert "7 failed" in rendered, "the failure count must remain in the header"


def test_failure_lines_persist_while_no_success_follows() -> None:
    """Consecutive failures stay visible until a success arrives."""
    from src.channels.activity import Activity, render_activity

    activity = Activity()
    for _ in range(3):
        activity.note_call("get_market_data")
        activity.note_result("get_market_data", ok=False, duration_ms=30)

    rendered = render_activity(activity, 0)
    assert rendered.count("⚠️") == 3
    assert "3 failed" in rendered


def test_a_failure_after_a_success_starts_a_fresh_row() -> None:
    """Clearing is per-success; a later failure is shown again."""
    from src.channels.activity import Activity, render_activity

    activity = Activity()
    activity.note_call("get_market_data")
    activity.note_result("get_market_data", ok=False, duration_ms=30)
    activity.note_call("search_symbol")
    activity.note_result("search_symbol", ok=True, duration_ms=100)
    assert "⚠️" not in render_activity(activity, 0)

    activity.note_call("get_market_data")
    activity.note_result("get_market_data", ok=False, duration_ms=30)
    rendered = render_activity(activity, 0)
    assert rendered.count("⚠️") == 1
    assert "2 failed" in rendered, "the count accumulates across clears"


def test_only_telegram_overrides_send_activity() -> None:
    """The default is a no-op, so the manager routes _activity to Telegram only."""
    from src.channels.base import BaseChannel

    assert TelegramChannel.overrides_send_activity() is True
    assert BaseChannel.overrides_send_activity() is False


def test_rich_card_keeps_header_and_meta_on_separate_lines() -> None:
    """The tool meta must appear once, under the header — never twice.

    Reproduces the reported crowding: the header trailed the tool labels AND
    the blockquote repeated them, so one Telegram line read
    ``Working · counts · failed · bash read grep``. The header is now counts
    only and the labels live solely in the quote block.
    """
    from src.channels.activity import Activity, render_activity_rich

    activity = Activity()
    activity.note_call("read_file")
    activity.note_result("read_file", ok=True, duration_ms=40)
    activity.note_call("grep_search")
    activity.note_result("grep_search", ok=True, duration_ms=120)
    activity.note_call("run_in_terminal")
    activity.note_result("run_in_terminal", ok=True, duration_ms=800)
    activity.narration = "Checking the figures\u2026"

    rendered = render_activity_rich(activity, 0)
    header, _, rest = rendered.partition("\n")

    # Header carries status + counts only; no tool chips bleed onto it.
    assert "Working" in header
    assert "3 actions" in header
    for label in ("read", "grep", "bash"):
        assert label not in header, f"{label!r} must not crowd the header"

    # Chips are Telegram-native <code>, not literal backticks, and appear once.
    assert "`" not in rendered, "markdown backticks leak into the HTML card"
    assert "<blockquote>" in rest
    assert "<code>" in rest
    assert rendered.count("<code>read</code>") == 1
    assert rendered.count("<code>grep</code>") == 1


def test_rich_card_without_tools_is_a_bare_thinking_line() -> None:
    """No tool yet -> one line, no empty quote block trailing it."""
    from src.channels.activity import Activity, render_activity_rich

    rendered = render_activity_rich(Activity(), 0)
    assert rendered.count("\n") == 0
    assert "blockquote" not in rendered
    assert "Thinking" in rendered

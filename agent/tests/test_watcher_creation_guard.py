"""Contracts for the watcher creation guard and the Telegram board auto-refresh.

Two behaviors are pinned here:

1. ``WatcherManagerTool`` rejects an incomplete watcher BEFORE it is persisted,
   with a message that names the offending field and a correction. The failure
   this prevents is a watcher that validates, persists, then fails every fetch
   forever while never firing (ported from OpenAlice's ``watchSpecSchema``).

2. The Telegram ``/watchers`` board auto-refreshes in place with no cap on the
   number of watchers shown, and the refresh loop is cancelled on Close and on
   channel stop so no orphan task edits a dead message.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.tools.watcher_tool import SUPPORTED_WATCH_INTERVALS, WatcherManagerTool
from src.watcher.market_data import DEFAULT_WATCH_SOURCE, normalize_source


def _base(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pair": "BTCUSDT",
        "direction": "LONG",
        "timeframe": "15M",
        "waiting_trigger": "15M closes below 79 then retests",
        "trigger_rules": [{"type": "price_breaks_level", "side": "below", "level": 79}],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Creation guard
# --------------------------------------------------------------------------- #


def test_valid_watcher_passes_guard() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base())
    assert error is None
    assert item is not None
    assert item.trigger_rules == [{"type": "price_breaks_level", "side": "below", "level": 79.0}]


def test_missing_pair_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(pair=""))
    assert error is not None
    assert "pair" in error.lower()


def test_source_prefixed_pair_rejected() -> None:
    """The exact mistake OpenAlice hit: ``yfinance|SOLUSD`` is not a pair."""
    _, error = WatcherManagerTool._item_from_kwargs(_base(pair="yfinance|SOLUSD"))
    assert error is not None
    assert "source prefix" in error.lower()


def test_venue_symbols_pass_the_pair_guard() -> None:
    """The guard must accept every spelling the loader layer routes.

    ``GC=F`` (Yahoo continuous futures) is a real, priceable instrument; the
    old ``isalnum()`` check refused it and told the model gold could not be
    watched at all. Each case here mirrors a rule in
    ``src.market_data._SOURCE_PATTERNS``.
    """
    for pair in (
        "GC=F",
        "CL=F",
        "EURUSD=X",
        "^SPX",
        "AAPL.US",
        "0700.HK",
        "VOD.L",
        "BTC-USDT",
        "XAUUSD.FX",
        "BTCUSDT",
    ):
        _, error = WatcherManagerTool._item_from_kwargs(_base(pair=pair))
        assert error is None, f"{pair} should be a valid watcher pair: {error}"


def test_malformed_pair_rejected_with_spelling_help() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(pair="@@@"))
    assert error is not None
    assert "invalid_pair" in error
    # The correction names the shapes that DO work, so a retry is not a guess.
    assert "GC=F" in error


def test_path_separator_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(pair="../../etc/passwd"))
    assert error is not None
    assert "invalid_pair" in error


def test_omitted_asset_class_is_inferred_from_the_symbol() -> None:
    """GC=F is gold, not a coin. The label feeds the board card and wake prompt."""
    cases = {
        "GC=F": "commodity",
        "CL=F": "commodity",
        "ES=F": "index",
        "EURUSD=X": "forex",
        "XAUUSD=X": "commodity",
        "^SPX": "index",
        "AAPL.US": "equity",
        "XAUUSD": "commodity",
        "GBPJPY": "forex",
        "BTCUSDT": "crypto",
    }
    for pair, expected in cases.items():
        item, error = WatcherManagerTool._item_from_kwargs(_base(pair=pair))
        assert error is None, f"{pair}: {error}"
        assert item is not None
        assert item.asset_class == expected, f"{pair} -> {item.asset_class}, expected {expected}"


def test_unsupported_timeframe_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(timeframe="2h"))
    assert error is not None
    assert "unsupported_timeframe" in error
    for interval in SUPPORTED_WATCH_INTERVALS:
        assert interval in error


def test_missing_trigger_rule_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(trigger_rules=[], entry_zone=None))
    assert error is not None
    assert "missing_trigger_rule" in error


def test_missing_waiting_trigger_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(waiting_trigger=""))
    assert error is not None
    assert "missing_waiting_trigger" in error


def test_zone_only_watcher_derives_a_rule() -> None:
    """A zone with no explicit rules still passes: a rule is derived from it."""
    item, error = WatcherManagerTool._item_from_kwargs(
        _base(trigger_rules=[], entry_zone={"low": 66.0, "high": 67.0})
    )
    assert error is None
    assert item is not None
    assert item.trigger_rules == [{"type": "price_enters_zone", "low": 66.0, "high": 67.0}]


def test_every_supported_interval_is_accepted() -> None:
    for interval in SUPPORTED_WATCH_INTERVALS:
        _, error = WatcherManagerTool._item_from_kwargs(_base(timeframe=interval))
        assert error is None, f"{interval} should be accepted: {error}"


def test_empty_timeframe_defaults_cleanly() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base(timeframe=""))
    assert error is None
    assert item is not None


def test_tool_description_lists_the_guard_contract() -> None:
    """The authoring instructions ride with the tool so the model sees them."""
    desc = WatcherManagerTool.description
    assert "pair" in desc
    assert "timeframe" in desc
    assert "trigger_rules" in desc
    assert "NEVER prefix a source" in desc


# --------------------------------------------------------------------------- #
# Source selection
# --------------------------------------------------------------------------- #


def test_default_source_is_yfinance() -> None:
    assert DEFAULT_WATCH_SOURCE == "yfinance"
    assert normalize_source("") == "yfinance"


def test_source_normalized_to_lowercase() -> None:
    assert normalize_source("OKX") == "okx"
    assert normalize_source("  Binance  ") == "binance"


def test_failed_spot_metal_fetch_names_the_contract() -> None:
    """An unpriceable spelling must say which spelling works.

    ``XAUUSD`` has no yfinance series; the caller reported "this instrument
    cannot be monitored" instead of retrying with ``GC=F``.
    """
    from src.watcher.market_data import _symbol_hint

    assert "GC=F" in _symbol_hint("XAUUSD")
    assert "XAUUSD=X" in _symbol_hint("XAUUSD")
    assert "EURUSD=X" in _symbol_hint("EURUSD")
    # A spelling already in loader form needs no correction.
    assert _symbol_hint("GC=F") == ""
    assert _symbol_hint("EURUSD=X") == ""
    assert _symbol_hint("^SPX") == ""


def test_omitted_source_defaults_to_yfinance() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base())
    assert error is None
    assert item is not None
    assert item.source == "yfinance"


def test_explicit_source_is_kept() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base(source="okx"))
    assert error is None
    assert item is not None
    assert item.source == "okx"


def test_auto_source_is_allowed() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base(source="auto"))
    assert error is None
    assert item is not None
    assert item.source == "auto"


def test_unknown_source_rejected() -> None:
    _, error = WatcherManagerTool._item_from_kwargs(_base(source="not_a_real_venue"))
    assert error is not None
    assert "unknown_source" in error
    assert "yfinance" in error


def test_source_round_trips_through_dict() -> None:
    item, error = WatcherManagerTool._item_from_kwargs(_base(source="binance"))
    assert error is None
    assert item is not None
    from src.watcher.models import WatchItem

    restored = WatchItem.from_dict(item.to_dict())
    assert restored.source == "binance"


# --------------------------------------------------------------------------- #
# Telegram board: no cap + auto-refresh
# --------------------------------------------------------------------------- #


def _make_telegram() -> Any:
    from src.channels.bus.queue import MessageBus
    from src.channels.telegram import TelegramChannel, TelegramConfig

    return TelegramChannel(TelegramConfig(enabled=True, token="x", inline_keyboards=True), MessageBus())


def test_board_lists_every_watcher(monkeypatch: Any) -> None:
    """The board must not silently cap at 50: it shows the whole workspace."""
    import src.channels.telegram as tg

    seen: dict[str, Any] = {}

    class _FakeStore:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def list(self, *, include_terminal: bool = True, limit: int = 200) -> list[Any]:
            seen["limit"] = limit
            return []

    import src.watcher.store as store_module

    monkeypatch.setattr(store_module, "WatcherStore", _FakeStore)
    monkeypatch.setattr(store_module, "default_watchers_dir", lambda: "/tmp/x")
    tg._load_watcher_state()
    assert seen["limit"] >= 1_000_000


def test_refresh_loop_starts_and_is_cancellable(monkeypatch: Any) -> None:
    """Starting the board arms a loop; Close cancels it with no orphan task."""
    channel = _make_telegram()
    channel._watch_board_state["1"] = {"index": 0, "expanded": set(), "message_id": 42}
    monkeypatch.setattr(channel, "_watch_refresh_seconds", lambda: 3600.0)

    async def _run() -> None:
        channel._start_watch_refresh("1")
        task = channel._watch_board_state["1"].get("refresh_task")
        assert task is not None and not task.done()
        channel._cancel_watch_refresh("1")
        assert channel._watch_board_state["1"]["refresh_task"] is None
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        assert task.cancelled() or task.done()

    asyncio.run(_run())


def test_refresh_does_not_stack_loops(monkeypatch: Any) -> None:
    channel = _make_telegram()
    channel._watch_board_state["1"] = {"index": 0, "expanded": set(), "message_id": 42}
    monkeypatch.setattr(channel, "_watch_refresh_seconds", lambda: 3600.0)

    async def _run() -> None:
        channel._start_watch_refresh("1")
        first = channel._watch_board_state["1"]["refresh_task"]
        channel._start_watch_refresh("1")
        second = channel._watch_board_state["1"]["refresh_task"]
        assert first is second
        channel._cancel_watch_refresh("1")
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await first

    asyncio.run(_run())


def test_refresh_requires_a_message_id() -> None:
    """A board with no sent message must not spawn a loop."""
    channel = _make_telegram()
    channel._watch_board_state["1"] = {"index": 0, "expanded": set()}
    channel._start_watch_refresh("1")
    assert channel._watch_board_state["1"].get("refresh_task") is None

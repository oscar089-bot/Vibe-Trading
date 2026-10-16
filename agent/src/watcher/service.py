"""Background watcher service.

REWRITTEN for this codebase (not copied from the v0.1.6 fork, which used a
bare ``threading.Thread``). Here the loop is an asyncio task following the
same shape as :class:`src.scheduled_research.executor.ScheduledResearchExecutor`:
idempotent ``start``/``stop``, an injectable clock, and a wakeup event so
shutdown never blocks for a full tick.

Cost discipline (the whole point of the design): the hot path is ONE market
fetch per active watcher per tick plus pure arithmetic. No agent run is created
while a condition is false. A watcher that never fires costs a price lookup
every ``poll_seconds`` and nothing more.

The loop is deliberately separate from the scheduled-research executor: a slow
quote fetch must never delay a calendar fire, and a calendar fire must never
delay a market reaction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from src.watcher.evaluator import EvaluatorConfig, MarketSnapshot, TriggerEvaluator
from src.watcher.market_data import MarketDataProvider, RoutedMarketDataProvider
from src.watcher.models import WatchItem, WatchStatus, WatcherEvent, utc_now_iso
from src.watcher.store import WatcherStore

logger = logging.getLogger(__name__)

#: Default cadence. Matches the target codebase's scheduler philosophy (cheap,
#: 60s-class ticks) rather than the fork's 30s; the re-entrancy guard means a
#: slow fetch delays the next pass instead of stacking one.
DEFAULT_POLL_SECONDS = 30.0

#: Per-fetch ceiling. A hung vendor must not stall the whole tick. Deliberately
#: generous (delayed vendor feeds can legitimately take many seconds), because a
#: too-tight ceiling makes a healthy watcher look permanently broken.
DEFAULT_FETCH_TIMEOUT_SECONDS = 20.0

#: Backoff bounds for a repeatedly failing market fetch. The first failures
#: retry at BASE, then the delay doubles per consecutive failure up to MAX.
#: Without this a chronically broken feed is re-fetched every tick forever.
DEFAULT_BACKOFF_BASE_SECONDS = 60.0
DEFAULT_BACKOFF_MAX_SECONDS = 30 * 60.0

#: Consecutive failures before the backoff starts doubling.
_BACKOFF_FREE_FAILURES = 2

#: How many watchers one tick may fetch before deferring the rest. Bounds a
#: pathological workspace with hundreds of watchers.
DEFAULT_MAX_FETCHES_PER_TICK = 50


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


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class WatchFire:
    """Why a watcher fired, handed to the fire callback."""

    watcher: WatchItem
    events: list[WatcherEvent]
    snapshot: MarketSnapshot
    terminal: bool


#: Called once per non-empty event batch. Must not raise; the service isolates
#: and logs any failure so one bad consumer cannot stop the poll loop.
FireCallback = Callable[[WatchFire], Awaitable[None]]


class WatcherService:
    """Poll active watchers and persist deterministic state transitions."""

    def __init__(
        self,
        *,
        store: WatcherStore,
        market_data: Optional[MarketDataProvider] = None,
        evaluator: Optional[TriggerEvaluator] = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        fetch_timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
        max_fetches_per_tick: int = DEFAULT_MAX_FETCHES_PER_TICK,
        on_fire: Optional[FireCallback] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        enabled: bool = True,
    ) -> None:
        self.store = store
        self.market_data = market_data or RoutedMarketDataProvider()
        self.evaluator = evaluator or TriggerEvaluator()
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.fetch_timeout_seconds = max(1.0, float(fetch_timeout_seconds))
        self.max_fetches_per_tick = max(1, int(max_fetches_per_tick))
        self.on_fire = on_fire
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._enabled = enabled
        self._task: Optional[asyncio.Task] = None
        self._wakeup: Optional[asyncio.Event] = None
        self._stopping = False
        # Re-entrancy guard: a fetch that outlives the interval delays the next
        # pass rather than stacking one.
        self._ticking = False

    @property
    def is_running(self) -> bool:
        """Return whether the background loop task is active."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the background loop.

        Idempotent. When disabled, this is a no-op.
        """
        if not self._enabled or self.is_running:
            return
        self._stopping = False
        self._wakeup = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="watcher-service")

    async def stop(self) -> None:
        """Stop the background loop and wait for it to finish.

        Idempotent. When disabled or not started, this is a no-op.
        """
        if not self._enabled:
            return
        task = self._task
        if task is None:
            return
        self._stopping = True
        if self._wakeup is not None:
            self._wakeup.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("watcher service tick failed", exc_info=True)
            if self._stopping:
                break
            await self._sleep_or_wake(self.poll_seconds)

    async def _sleep_or_wake(self, sleep_seconds: float) -> None:
        wakeup = self._wakeup
        if wakeup is None:
            await asyncio.sleep(sleep_seconds)
            return
        if self._stopping:
            return
        wakeup.clear()
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=sleep_seconds)
        except asyncio.TimeoutError:
            pass

    async def tick(self) -> list[WatcherEvent]:
        """Poll every active watcher once.

        A no-op when a previous tick is still in flight (re-entrancy guard).

        Returns:
            Every event emitted this pass, across all polled watchers.
        """
        if self._ticking:
            return []
        self._ticking = True
        emitted: list[WatcherEvent] = []
        try:
            items = self.store.list(include_terminal=False, limit=self.max_fetches_per_tick)
            for item in items:
                if not item.is_pollable:
                    continue
                try:
                    events = await self.poll_one(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("watcher %s poll failed", item.id, exc_info=True)
                    events = []
                emitted.extend(events)
        finally:
            self._ticking = False
        return emitted

    async def poll_one(self, item: WatchItem) -> list[WatcherEvent]:
        """Poll one watcher once and persist the outcome."""
        now = self._now_fn()

        # A persisted backoff means a failing feed is skipped without a fetch.
        backoff_until = _parse_dt(item.watcher_state.backoff_until)
        if backoff_until is not None and backoff_until > now:
            return []

        try:
            snapshot = await self._fetch_snapshot(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._record_error(item, exc, now)

        item.watcher_state.error_count = 0
        item.watcher_state.last_error = None
        item.watcher_state.backoff_until = None
        result = self.evaluator.evaluate(item, snapshot)
        self.store.update(result.watcher)
        for event in result.events:
            self.store.append_event(event)
        if result.events:
            await self._notify(result.watcher, result.events, snapshot)
        return result.events

    async def _fetch_snapshot(self, item: WatchItem) -> MarketSnapshot:
        """Fetch the market snapshot off the event loop with a timeout."""
        return await asyncio.wait_for(
            asyncio.to_thread(
                self.market_data.latest_price,
                item.pair,
                asset_class=item.asset_class,
                timeframe=item.timeframe or "",
                source=getattr(item, "source", "") or "",
            ),
            timeout=self.fetch_timeout_seconds,
        )

    def _record_error(self, item: WatchItem, exc: Exception, now: datetime) -> list[WatcherEvent]:
        """Persist a fetch failure and schedule the next backoff."""
        item.watcher_state.error_count += 1
        item.watcher_state.last_error = f"{type(exc).__name__}: {exc}"
        item.watcher_state.last_checked = utc_now_iso()
        item.watcher_state.last_event = "ERROR"
        delay = self._next_backoff_seconds(item.watcher_state.error_count)
        if delay is not None:
            item.watcher_state.backoff_until = _iso(now + timedelta(seconds=delay))
        self.store.update(item)
        # Emit an ERROR event only on the first failure and every tenth after,
        # so a long outage does not flood the event log.
        if item.watcher_state.error_count != 1 and item.watcher_state.error_count % 10 != 0:
            return []
        event = WatcherEvent(
            watcher_id=item.id,
            event_type="ERROR",
            message=f"Watcher data fetch failed for {item.pair}: {exc}",
            data={"error": str(exc)},
        )
        self.store.append_event(event)
        return [event]

    @staticmethod
    def _next_backoff_seconds(error_count: int) -> Optional[float]:
        """Return the next backoff delay, or None inside the free-retry window."""
        if error_count <= _BACKOFF_FREE_FAILURES:
            return None
        steps = min(error_count - _BACKOFF_FREE_FAILURES - 1, 20)
        return min(DEFAULT_BACKOFF_BASE_SECONDS * 2 ** steps, DEFAULT_BACKOFF_MAX_SECONDS)

    async def _notify(
        self, item: WatchItem, events: list[WatcherEvent], snapshot: MarketSnapshot
    ) -> None:
        """Hand a fired watcher to the injected callback.

        The callback owns its own retry story; a failure here is logged and
        never stops the loop, so the Discord/Telegram alert and the session
        wake can fail independently of each other.
        """
        if self.on_fire is None:
            return
        fire = WatchFire(
            watcher=item,
            events=list(events),
            snapshot=snapshot,
            terminal=bool(item.watcher_state.terminal),
        )
        try:
            await self.on_fire(fire)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("watcher fire callback failed for %s", item.id, exc_info=True)

    # --- convenience wrappers over the store ---

    def create(self, item: WatchItem) -> WatchItem:
        return self.store.create(item)

    def get(self, watcher_id: str) -> Optional[WatchItem]:
        return self.store.get(watcher_id)

    def list(self, *, include_terminal: bool = True, limit: int = 200) -> list[WatchItem]:
        return self.store.list(include_terminal=include_terminal, limit=limit)

    def update(self, item: WatchItem) -> None:
        self.store.update(item)

    def set_status(self, watcher_id: str, status: WatchStatus, *, reason: str = "") -> Optional[WatchItem]:
        return self.store.set_status(watcher_id, status, reason=reason)

    def delete(self, watcher_id: str) -> bool:
        return self.store.delete(watcher_id)

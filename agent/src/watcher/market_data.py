"""Market-data adapter for watcher polling.

REWRITTEN for this codebase (not copied from the v0.1.6 fork). The fork's
provider only knew OKX + yfinance and hard-coded both; here the adapter is a
thin shim over :func:`src.market_data.fetch_market_data`, so watchers inherit
the full ordered-fallback loader registry — crypto, forex, equities, futures
and every optional-key source — for free.

Contract: :meth:`RoutedMarketDataProvider.latest_price` returns a
:class:`~src.watcher.evaluator.MarketSnapshot` whose ``candles`` are the recent
bars for the requested timeframe. The most recent bar is marked
``confirmed: False`` when its close time is still in the future, so a
``candle_close`` rule never acts on a forming candle.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List, Protocol

from src.watcher.evaluator import MarketSnapshot
from src.watcher.models import utc_now_iso

logger = logging.getLogger(__name__)

#: How many bars to request. Enough for a candle_close rule to see the prior
#: candle plus the one that just closed, with headroom.
_DEFAULT_BARS = 200

#: Interval passed to the loader layer when a watcher declares no timeframe.
_DEFAULT_INTERVAL = "1D"

#: Source a watcher polls through when it declares none. yfinance is the
#: agent's default quote venue and needs no API key, so an omitted source
#: still resolves for crypto/equity/forex/index without extra configuration.
DEFAULT_WATCH_SOURCE = "yfinance"


def normalize_source(value: str) -> str:
    """Normalize a watcher source label to a canonical ``VALID_SOURCES`` name.

    Empty falls back to :data:`DEFAULT_WATCH_SOURCE`. ``auto`` is preserved so a
    watcher can still ask the loader registry to route by market. Unknown names
    are returned lower-cased; the market-data layer rejects them at fetch time
    with a clear error, and the creation guard validates against the registry
    before persist.
    """
    raw = str(value or "").strip().lower()
    return raw or DEFAULT_WATCH_SOURCE


class MarketDataProvider(Protocol):
    """Protocol for watcher price providers."""

    def latest_price(
        self,
        pair: str,
        *,
        asset_class: str = "crypto",
        timeframe: str = "",
        source: str = "",
    ) -> MarketSnapshot:
        """Return the latest price snapshot for a pair."""


def normalize_timeframe(value: str) -> str:
    """Normalize common watcher timeframe labels into compact interval labels."""
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
        "1day": "1d",
        "daily": "1d",
    }
    return aliases.get(raw, raw)


def _timeframe_seconds(value: str) -> int:
    """Parse a compact interval label (``1m``/``1h``/``1d``/``1w``) to seconds."""
    raw = normalize_timeframe(value)
    match = re.fullmatch(r"(\d+)([mhdwM])", raw)
    if not match:
        return 0
    count = int(match.group(1))
    unit = match.group(2)
    factors = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "M": 30 * 86400}
    return count * factors.get(unit, 0)


def _loader_interval(value: str) -> str:
    """Map a watcher timeframe label onto the loader layer's interval token.

    The loader registry accepts lowercase ``1h``/``4h``/``1d``/``1w`` aliases
    and uppercase ``1M`` for monthly; ``1m`` stays minute. Empty falls back to
    daily.
    """
    normalized = normalize_timeframe(value)
    if not normalized:
        return _DEFAULT_INTERVAL
    if normalized in {"1m", "5m", "15m", "30m"}:
        return normalized
    if normalized.endswith("w"):
        return normalized.upper()  # 1w -> 1W
    if normalized.endswith("d"):
        return normalized.upper()  # 1d -> 1D
    if normalized.endswith("h"):
        return normalized.upper()  # 1h -> 1H, 4h -> 4H
    return normalized


def _date_window(timeframe: str, *, bars: int = _DEFAULT_BARS) -> tuple[str, str]:
    """Return ``(start_date, end_date)`` wide enough to yield ``bars`` bars."""
    now = datetime.now(timezone.utc)
    seconds = _timeframe_seconds(timeframe) or 86400
    span = timedelta(seconds=seconds * bars)
    # Calendar-day granularity with a one-day cushion; intraday loaders honour
    # the day range and return the bars within it.
    start = (now - span - timedelta(days=1)).date().isoformat()
    end = (now + timedelta(days=1)).date().isoformat()
    return start, end


#: Precious-metal base codes and the Yahoo continuous-futures root that tracks
#: the same metal. yfinance exposes no bare ``XAUUSD``; the contract is ``GC=F``.
_METAL_FUTURES_ROOTS = {"XAU": "GC", "XAG": "SI", "XPT": "PL", "XPD": "PA"}


def _symbol_hint(pair: str) -> str:
    """Return a correction hint naming a spelling the loader layer can price.

    A failed fetch of ``XAUUSD`` or ``EURUSD`` used to read as "no market data",
    which the caller reasonably reported as "this instrument cannot be
    monitored". The instrument is fine; the spelling is not. yfinance serves the
    continuous contract as ``GC=F`` and the FX pair as ``EURUSD=X``, so name it.
    """
    token = str(pair or "").strip().upper()
    bare = token.replace("/", "").replace("-", "").replace("_", "")
    if token.endswith(("=F", "=X", ".FX")) or "^" in token:
        return ""
    if len(bare) == 6 and bare.isalpha():
        base = bare[:3]
        root = _METAL_FUTURES_ROOTS.get(base)
        if root:
            return (
                f" {pair!r} is a spot metal; yfinance exposes the continuous contract "
                f"as {root}=F (and the FX cross as {bare}=X). Retry with one of those."
            )
        return f" {pair!r} looks like an FX pair; yfinance spells it {bare}=X."
    return ""


def _candle_from_row(timestamp: Any, row: Any, timeframe: str, *, now: datetime) -> dict[str, Any]:
    """Build one candle dict from a DataFrame row."""
    if isinstance(timestamp, datetime):
        opened_at = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    else:
        opened_at = datetime.fromisoformat(str(timestamp))
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
    candle: dict[str, Any] = {
        "timestamp": opened_at.astimezone(timezone.utc).isoformat(),
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "timeframe": timeframe,
    }
    if "volume" in row and row["volume"] is not None:
        try:
            candle["volume"] = float(row["volume"])
        except (TypeError, ValueError):
            pass
    # A bar whose close time is still in the future is the forming candle.
    seconds = _timeframe_seconds(timeframe)
    if seconds > 0:
        closes_at = opened_at + timedelta(seconds=seconds)
        candle["confirmed"] = closes_at <= now
    else:
        candle["confirmed"] = True
    return candle


class RoutedMarketDataProvider:
    """Market-data provider backed by the repository loader registry."""

    def __init__(self, *, bars: int = _DEFAULT_BARS) -> None:
        self.bars = max(2, int(bars))

    def latest_price(
        self,
        pair: str,
        *,
        asset_class: str = "crypto",
        timeframe: str = "",
        source: str = "",
    ) -> MarketSnapshot:
        """Return the latest price and recent candles for ``pair``.

        ``source`` selects the market-data venue. Empty falls back to
        :data:`DEFAULT_WATCH_SOURCE` (yfinance); ``"auto"`` lets the loader
        registry route by market. An explicit source walks its own fallback
        chain (e.g. ``okx`` -> ``binance`` -> ``ccxt`` -> ``yfinance``).

        Raises:
            RuntimeError: When no source returned data for the symbol, so the
                caller can count the failure and back off. The message names
                the requested symbol, timeframe and source.
        """
        from src.market_data import fetch_market_data

        normalized_tf = normalize_timeframe(timeframe)
        resolved_source = normalize_source(source)
        start_date, end_date = _date_window(normalized_tf, bars=self.bars)
        payload = fetch_market_data(
            codes=[pair],
            start_date=start_date,
            end_date=end_date,
            source=resolved_source,
            interval=_loader_interval(normalized_tf),
            include_provenance=True,
            as_frames=True,
        )
        hint = _symbol_hint(pair)
        if pair in payload.get("_unresolved", []):
            raise RuntimeError(
                f"no market data for {pair!r} "
                f"(timeframe={normalized_tf or _DEFAULT_INTERVAL}, source={resolved_source}).{hint}"
            )
        frame = payload.get(pair)
        if frame is None or getattr(frame, "empty", True):
            raise RuntimeError(
                f"no market data for {pair!r} "
                f"(timeframe={normalized_tf or _DEFAULT_INTERVAL}, source={resolved_source}).{hint}"
            )

        now = datetime.now(timezone.utc)
        candles: List[dict[str, Any]] = []
        for timestamp, row in frame.iterrows():
            try:
                candles.append(_candle_from_row(timestamp, row, normalized_tf, now=now))
            except (KeyError, TypeError, ValueError):
                continue
        if not candles:
            raise RuntimeError(f"market data for {pair!r} carried no usable bars")

        last = candles[-1]
        provenance = (payload.get("_provenance") or {}).get(pair) or {}
        return MarketSnapshot(
            pair=pair,
            price=float(last["close"]),
            timestamp=utc_now_iso(),
            asset_class=str(provenance.get("asset_class") or asset_class or "crypto"),
            timeframe=normalized_tf or None,
            candles=candles,
        )

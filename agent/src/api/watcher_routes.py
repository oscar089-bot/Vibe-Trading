"""Watcher HTTP routes.

Mounted by ``agent/api_server.py`` via ``register_watcher_routes(app, ...)``.
Also owns the watcher background-service singleton and its startup/shutdown
helpers, mirroring the scheduled-research executor's wiring.

The fire path is built here: a fired watcher wakes its owning session through
the session runtime and, independently, sends a machine alert through the
configured IM channel. Neither path can block the other or the poll loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys as _sys
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status

from src.config.accessor import get_env_config
from src.watcher.models import WatchItem, WatchStatus, WatcherEvent
from src.watcher.service import WatcherService
from src.watcher.store import WatcherStore, default_watchers_dir
from src.watcher.wake import WatcherDispatcher

logger = logging.getLogger(__name__)

AuthDep = Callable[..., Awaitable[Any]]

#: Module-level service singleton, created on first access.
_watcher_service: Optional[WatcherService] = None


# ---------------------------------------------------------------------------
# Fire-path collaborators
# ---------------------------------------------------------------------------


def _host_module() -> Any:
    """Return the running ``api_server`` module, or None."""
    return _sys.modules.get("api_server") or _sys.modules.get("agent.api_server")


async def _wake_session(session_id: str, prompt: str) -> bool:
    """Wake the owning session through the session runtime.

    Returns:
        True when the attempt was queued, False when the session runtime is
        unavailable so the caller can log a skipped wake rather than a crash.
    """
    host = _host_module()
    svc = host._get_session_service() if host and hasattr(host, "_get_session_service") else None
    if svc is None:
        return False
    result = await svc.send_message(session_id, prompt)
    attempt_id = result.get("attempt_id") if isinstance(result, dict) else None
    if attempt_id:
        # Fire-and-forget: the woken attempt finishes later, and its reply is
        # what the operator reads on their phone. Mirrors the fork's Discord
        # flow, where the re-analysis lands in the same channel as the alert.
        asyncio.create_task(
            _deliver_reanalysis_when_done(session_id, attempt_id),
            name=f"watcher-reanalysis-{session_id}",
        )
    return True


async def _deliver_reanalysis_when_done(
    session_id: str, attempt_id: str, *, timeout_seconds: float = 900.0, poll_seconds: float = 5.0
) -> None:
    """Deliver a woken attempt's final reply to the watcher channel.

    Polls the session for the assistant message linked to ``attempt_id`` and
    sends it through the same channel the machine alert uses, so the operator
    sees the alert and then the model's re-analysis — the two-message flow the
    Discord bot already provided.

    Best effort: a timeout or a delivery failure is logged, never raised.
    """
    import time as _time

    host = _host_module()
    svc = host._get_session_service() if host and hasattr(host, "_get_session_service") else None
    if svc is None:
        return
    deadline = _time.monotonic() + timeout_seconds
    while _time.monotonic() < deadline:
        try:
            messages = svc.get_messages(session_id, limit=200)
        except Exception:
            messages = []
        for message in reversed(messages):
            if message.role != "assistant":
                continue
            if message.linked_attempt_id != attempt_id:
                continue
            content = (message.content or "").strip()
            if not content:
                continue
            try:
                await _send_alert(content)
            except Exception:
                logger.warning(
                    "watcher re-analysis delivery failed for session %s", session_id, exc_info=True
                )
            return
        # No reply yet. A failed/cancelled/interrupted attempt writes NO assistant
        # message, so without this check the loop would wait out the full timeout
        # and the operator would never learn the re-analysis died.
        failure = _terminal_failure_notice(svc, session_id, attempt_id)
        if failure is not None:
            try:
                await _send_alert(failure)
            except Exception:
                logger.warning(
                    "watcher failure notice delivery failed for session %s", session_id, exc_info=True
                )
            return
        await asyncio.sleep(poll_seconds)
    logger.warning("watcher re-analysis for session %s did not finish in time", session_id)


def _terminal_failure_notice(svc: Any, session_id: str, attempt_id: str) -> Optional[str]:
    """Return an error notice when an attempt ended without producing a reply.

    Reads the attempt record directly: a failed/cancelled/interrupted run
    writes no assistant message, so the message poll alone would wait out the
    whole timeout. Returns None while the attempt is still running (or when its
    status cannot be read), so the caller keeps polling. ``completed`` is
    deliberately not terminal here — the reply is appended moments after the
    status flips.
    """
    try:
        attempt = svc.store.get_attempt(session_id, attempt_id)
    except Exception:
        return None
    if attempt is None:
        return None
    status = getattr(getattr(attempt, "status", None), "value", None)
    if status not in {"failed", "cancelled", "interrupted"}:
        return None
    detail = str(getattr(attempt, "error", "") or "").strip()
    if status == "cancelled":
        head = "\u26a0\ufe0f Watcher re-analysis was cancelled."
    elif status == "interrupted":
        head = "\u26a0\ufe0f Watcher re-analysis was interrupted (the server restarted)."
    else:
        head = "\u26a0\ufe0f Watcher re-analysis FAILED."
    if detail:
        head += f"\n\n{detail}"
    return head


async def _send_alert(text: str) -> None:
    """Send the machine alert to the configured watcher channel.

    Best effort: when no channel/target is configured the alert is silently
    skipped (the wake still happens). A configured-but-unreachable channel
    raises so the dispatcher can log it.
    """
    tuning = get_env_config().agent_tuning
    channel = str(getattr(tuning, "vibe_trading_watcher_channel", "") or "").strip()
    target = str(getattr(tuning, "vibe_trading_watcher_channel_target", "") or "").strip()
    if not channel or not target:
        return
    host = _host_module()
    manager = getattr(host, "_channel_manager", None) if host else None
    if manager is None:
        return
    adapter = manager.get_channel(channel)
    if adapter is None:
        raise RuntimeError(f"watcher alert channel {channel!r} is not configured")
    from src.channels.bus.events import OutboundMessage

    await adapter.send_with_receipt(
        OutboundMessage(
            channel=channel,
            chat_id=target,
            content=text,
            metadata={"force_send": True},
        )
    )


# ---------------------------------------------------------------------------
# Service singleton + lifecycle
# ---------------------------------------------------------------------------


def _watcher_enabled() -> bool:
    """Return whether the watcher service should run."""
    return bool(get_env_config().agent_tuning.vibe_trading_enable_watcher)


def _get_watcher_service() -> WatcherService:
    """Return the singleton WatcherService, creating it on first call."""
    global _watcher_service
    if _watcher_service is None:
        tuning = get_env_config().agent_tuning
        dispatcher = WatcherDispatcher(wake=_wake_session, alert=_send_alert)
        _watcher_service = WatcherService(
            store=WatcherStore(default_watchers_dir()),
            poll_seconds=float(getattr(tuning, "vibe_trading_watcher_poll_seconds", 30.0) or 30.0),
            fetch_timeout_seconds=float(
                getattr(tuning, "vibe_trading_watcher_fetch_timeout_seconds", 20.0) or 20.0
            ),
            max_fetches_per_tick=int(
                getattr(tuning, "vibe_trading_watcher_max_fetches_per_tick", 50) or 50
            ),
            on_fire=dispatcher.dispatch,
            enabled=True,
        )
    return _watcher_service


def _start_watcher_service() -> None:
    """Start watcher polling when the feature is enabled."""
    if not _watcher_enabled():
        return
    _get_watcher_service().start()


async def _stop_watcher_service() -> None:
    """Stop watcher polling if it was started."""
    if _watcher_service is not None:
        await _watcher_service.stop()


# ---------------------------------------------------------------------------
# Board projection
# ---------------------------------------------------------------------------


def watcher_board_item(item: WatchItem) -> Dict[str, Any]:
    """Return the UI-facing board row for one watcher.

    Reads the persisted record only; never re-fetches a price, so the board is
    an O(1) projection of state the loop already wrote.
    """
    state = item.watcher_state
    return {
        "id": item.id,
        "pair": item.pair,
        "asset_class": item.asset_class,
        "direction": item.direction,
        "timeframe": item.timeframe,
        "source": getattr(item, "source", "") or "yfinance",
        "status": item.status.value,
        "kind": item.kind.value,
        "entry_zone": item.entry_zone.to_dict() if item.entry_zone else None,
        "waiting_trigger": item.waiting_trigger,
        "trigger_rules": item.trigger_rules,
        "invalidation_rules": item.invalidation_rules,
        "analysis_context": dict(item.analysis_context or {}),
        "session_id": item.session_id or item.source_session_id,
        "is_pollable": bool(item.is_pollable),
        "last_price": state.last_price,
        "last_checked": state.last_checked,
        "last_market_timestamp": state.last_market_timestamp,
        "last_event": state.last_event,
        "armed_at": state.armed_at,
        "terminal": bool(state.terminal),
        "error_count": state.error_count,
        "last_error": state.last_error,
        "alerted_conditions": dict(state.alerted_conditions or {}),
        "fired_conditions": dict(state.fired_conditions or {}),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register_watcher_routes(
    app: FastAPI,
    require_auth: AuthDep | None = None,
) -> None:
    """Mount the watcher routes onto ``app``.

    Resolves ``require_auth`` from the host ``api_server`` module via
    ``sys.modules`` when not passed explicitly.
    """
    host = _host_module()
    if host is None:
        raise RuntimeError(
            "register_watcher_routes: api_server module not in sys.modules; "
            "ensure api_server is imported before calling this function"
        )
    if require_auth is None:
        require_auth = host.require_auth

    @app.get(
        "/watcher-board",
        dependencies=[Depends(require_auth)],
    )
    async def watcher_board(
        include_terminal: bool = Query(default=False),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        """Return every watcher as a UI board row."""
        svc = _get_watcher_service()
        items = svc.list(include_terminal=include_terminal, limit=limit)
        return {
            "enabled": _watcher_enabled(),
            "running": svc.is_running,
            "watchers": [watcher_board_item(item) for item in items],
            "count": len(items),
        }

    @app.get(
        "/watchers",
        dependencies=[Depends(require_auth)],
    )
    async def list_watchers(
        include_terminal: bool = Query(default=True),
        pair: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        """List watchers, optionally filtered by pair."""
        svc = _get_watcher_service()
        items = svc.list(include_terminal=include_terminal, limit=limit)
        if pair:
            wanted = pair.strip().upper()
            items = [item for item in items if item.pair.strip().upper() == wanted]
        return {
            "watchers": [watcher_board_item(item) for item in items],
            "count": len(items),
        }

    @app.get(
        "/watchers/{watcher_id}",
        dependencies=[Depends(require_auth)],
    )
    async def get_watcher(watcher_id: str) -> Dict[str, Any]:
        """Return one watcher and its recent event log."""
        _validate_watcher_id(watcher_id)
        svc = _get_watcher_service()
        item = svc.get(watcher_id)
        if item is None:
            raise HTTPException(status_code=404, detail="watcher not found")
        events = svc.store.read_events(watcher_id, limit=100)
        return {
            "watcher": watcher_board_item(item),
            "events": [event.to_dict() for event in events],
        }

    @app.get(
        "/watchers/{watcher_id}/events",
        dependencies=[Depends(require_auth)],
    )
    async def list_watcher_events(
        watcher_id: str,
        after_index: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> Dict[str, Any]:
        """Return the watcher's append-only event log."""
        _validate_watcher_id(watcher_id)
        svc = _get_watcher_service()
        if svc.get(watcher_id) is None:
            raise HTTPException(status_code=404, detail="watcher not found")
        events: List[WatcherEvent] = svc.store.read_events(
            watcher_id, after_index=after_index, limit=limit
        )
        return {"events": [event.to_dict() for event in events], "count": len(events)}

    @app.post(
        "/watchers/{watcher_id}/cancel",
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_auth)],
    )
    async def cancel_watcher(watcher_id: str) -> Dict[str, Any]:
        """Cancel a watcher so the loop stops polling it."""
        _validate_watcher_id(watcher_id)
        svc = _get_watcher_service()
        updated = svc.set_status(watcher_id, WatchStatus.CANCELLED, reason="Cancelled from the watcher board.")
        if updated is None:
            raise HTTPException(status_code=404, detail="watcher not found")
        return {"status": "ok", "watcher": watcher_board_item(updated)}

    @app.post(
        "/watchers/{watcher_id}/pause",
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_auth)],
    )
    async def pause_watcher(watcher_id: str) -> Dict[str, Any]:
        """Pause a watcher (kept as history, not polled)."""
        _validate_watcher_id(watcher_id)
        svc = _get_watcher_service()
        updated = svc.set_status(watcher_id, WatchStatus.PAUSED, reason="Paused from the watcher board.")
        if updated is None:
            raise HTTPException(status_code=404, detail="watcher not found")
        return {"status": "ok", "watcher": watcher_board_item(updated)}

    @app.delete(
        "/watchers/{watcher_id}",
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(require_auth)],
    )
    async def delete_watcher(watcher_id: str) -> Dict[str, Any]:
        """Delete a watcher permanently."""
        _validate_watcher_id(watcher_id)
        svc = _get_watcher_service()
        deleted = svc.delete(watcher_id)
        return {"status": "ok" if deleted else "not_found", "deleted": bool(deleted)}


def _validate_watcher_id(watcher_id: str) -> None:
    """Reject a watcher id that could escape the storage directory."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", watcher_id or ""):
        raise HTTPException(
            status_code=422,
            detail="watcher id must be 1-128 characters of letters, digits, '_' or '-'",
        )

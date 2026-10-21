"""Watcher management tool for the finance agent.

Ported from the Vibe-Trading (v0.1.6) watcher subsystem. The tool is rebased
on this codebase's :class:`src.agent.tools.BaseTool` conventions and resolves
its storage through :func:`src.watcher.store.default_watchers_dir` (the fork
read a module-level ``WATCHERS_DIR`` that does not exist here).
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.agent.tools import BaseTool
from src.watcher.models import (
    PriceZone,
    WatchItem,
    WatchKind,
    WatchStatus,
    WatcherEvent,
    WatcherState,
    utc_now_iso,
)
from src.watcher.store import WatcherStore, default_watchers_dir

#: Intervals the watcher market-data layer can actually serve. Kept in lockstep
#: with the loader registry's supported bars; a timeframe outside this set would
#: validate and then fail EVERY fetch forever, logging an error each tick without
#: ever firing. Rejecting it at authoring time makes the mistake visible instead
#: of silently burning fetches (ported from OpenAlice's isServableBarInterval).
SUPPORTED_WATCH_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")

#: Symbol shapes the market-data layer actually serves. Each entry mirrors a
#: routing rule in :data:`src.market_data._SOURCE_PATTERNS`, so a token matching
#: one is priceable by construction. The guard must therefore accept the
#: punctuation those venues require — ``=F`` (Yahoo continuous futures), ``=X``
#: (Yahoo FX), ``^`` (Yahoo indices), ``.`` (exchange suffixes) and ``-`` / ``/``
#: (exchange crypto pairs). Treating them as "characters a symbol cannot carry"
#: refused real instruments such as ``GC=F`` and told the model gold was
#: unmonitorable. Every pattern is anchored and dot-free at the base, so no
#: token can carry a path separator.
_SYMBOL_SHAPES = (
    re.compile(r"^\^[A-Z0-9][A-Z0-9.\-]*$"),                 # ^SPX, ^VIX
    re.compile(r"^[A-Z0-9]{2,}=(F|X)$"),                     # GC=F, CL=F, EURUSD=X
    re.compile(r"^[A-Z0-9][A-Z0-9&\-]*(\.[A-Z]{1,3})+$"),    # AAPL.US, 0700.HK, EURUSD.FX
    re.compile(r"^[A-Z0-9]{2,}[-/][A-Z0-9]{2,}$"),           # BTC-USDT, XAU/USD
    re.compile(r"^[A-Z0-9]{2,}$"),                            # AAPL, XAUUSD, BTCUSDT
)

#: Precious-metal base codes. A pair whose base is one of these quotes a metal,
#: not a currency, so it is labelled a commodity rather than FX.
_METAL_BASES = frozenset({"XAU", "XAG", "XPT", "XPD"})

#: Yahoo continuous-futures roots that track an equity index rather than a
#: physical commodity (ES=F S&P 500, NQ=F Nasdaq, YM=F Dow, RTY=F Russell).
_INDEX_FUTURES_ROOTS = frozenset({"ES", "NQ", "YM", "RTY", "MES", "MNQ", "MYM", "M2K"})

#: Precious metal -> Yahoo continuous-futures root, used to name the contract a
#: failed spot fetch should have asked for (XAU -> GC=F).
_METAL_FUTURES_ROOTS = {"XAU": "GC", "XAG": "SI", "XPT": "PL", "XPD": "PA"}


def infer_asset_class(pair: str) -> str:
    """Infer a watcher's asset class from its symbol spelling.

    Used only when the model omits ``asset_class``. This is a label, not a
    routing decision — :mod:`src.watcher.market_data` resolves the venue itself
    — but the label is what the board card and the wake prompt show, so
    ``GC=F`` must not be described as crypto.
    """
    token = str(pair or "").strip().upper()
    if not token:
        return "crypto"
    if token.startswith("^"):
        return "index"
    if token.endswith("=F"):
        root = token[:-2]
        return "index" if root in _INDEX_FUTURES_ROOTS else "commodity"
    if token.endswith("=X"):
        return "commodity" if token[:3] in _METAL_BASES else "forex"
    if token.endswith(".FX"):
        return "commodity" if token[:-3][:3] in _METAL_BASES else "forex"
    if "." in token:
        return "equity"
    for separator in ("/", "-"):
        if separator in token:
            base = token.split(separator, 1)[0]
            return "commodity" if base in _METAL_BASES else "crypto"
    if len(token) == 6 and token.isalpha():
        return "commodity" if token[:3] in _METAL_BASES else "forex"
    return "crypto"


class WatcherManagerTool(BaseTool):
    """List and mutate persistent market watchers."""

    name = "watcher_manager"
    description = (
        "List, inspect, create, update, or delete persistent market watchers. "
        "Use this before claiming a watcher was changed. Create/update/delete actions verify the watcher "
        "state before returning success. The tool refuses to recreate an already-triggered watcher with "
        "the same deterministic trigger; use a new next-condition trigger or leave VALID setups as triggered history. "
        "For multi-step plans such as 'retest zone, reject, then candle close', arm only the next unmet step.\n\n"
        "A COMPLETE watcher needs ALL of these or creation is rejected with a correction:\n"
        "  1. pair: the bare symbol only (BTCUSDT, GBPJPY, AAPL, GC=F). NEVER prefix a source "
        "(not 'yfinance|SOLUSD'). Yahoo venue conventions are accepted as written: '=F' continuous "
        "futures (GC=F, CL=F), '=X' FX (EURUSD=X, XAUUSD=X), '^' indices (^SPX), exchange suffixes "
        "(AAPL.US, 0700.HK, VOD.L) and 'XAUUSD.FX' for MT5/tickerall metals.\n"
        "  2. asset_class: crypto | forex | equity | commodity | index.\n"
        "  3. direction: LONG or SHORT.\n"
        "  4. timeframe: one of 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w (omit to default to 1d). A timeframe outside "
        "this set validates but fails every fetch forever, so it is rejected up front.\n"
        "  5. waiting_trigger: the condition in plain English (shown on the board and in the wake prompt).\n"
        "  6. trigger_rules: at least ONE fully-specified deterministic rule. Level rules need numeric "
        "'level' + 'side' (\"above\"/\"below\"); zone rules need numeric 'low' + 'high'. Never use 'condition'.\n"
        "  7. entry_zone (optional): {low, high} — when present and no rules are given, a price_enters_zone "
        "rule is derived from it.\n"
        "  8. source (optional): the market-data venue to poll through. Defaults to 'yfinance'. Pick a venue "
        "deliberately when the analysis used one — e.g. 'okx'/'binance'/'ccxt' for crypto, 'yfinance'/'yahoo' "
        "for equities/forex/index, 'tushare'/'akshare' for A-shares, 'mt5' for local forex. Use 'auto' to let "
        "the registry route by market. An unknown source is rejected.\n"
        "Provide numeric values, not strings. The board card and wake prompt read waiting_trigger, so write it "
        "so a human can tell what is being watched without the rule table."
    )
    #: Authoring guidance surfaced to the model on every call, so a watcher is
    #: built complete the first time rather than rejected and retried. Mirrors
    #: OpenAlice's watchSpecSchema contract: a watcher needs a servable pair, a
    #: servable timeframe, at least one deterministic trigger rule, and every
    #: level/zone rule fully specified with numbers.
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update", "delete"],
                "description": "Watcher action to run.",
            },
            "watcher_id": {
                "type": "string",
                "description": "Watcher id for get/update/delete.",
            },
            "pair": {
                "type": "string",
                "description": "Pair filter for list or pair for create/update, e.g. GBPJPY or BTCUSDT.",
            },
            "asset_class": {
                "type": "string",
                "description": "Asset class for create/update (crypto, forex, equity, commodity, index).",
            },
            "direction": {
                "type": "string",
                "description": "LONG or SHORT for create/update.",
            },
            "timeframe": {
                "type": "string",
                "description": "Primary watcher timeframe, e.g. 15M, 4H.",
            },
            "source": {
                "type": "string",
                "description": "Market-data venue to poll through (default 'yfinance'). "
                "Examples: 'yfinance', 'yahoo', 'okx', 'binance', 'ccxt', 'tushare', "
                "'akshare', 'mt5', or 'auto' to route by market. Must be a known loader source.",
            },
            "entry_zone": {
                "type": "object",
                "description": "Optional entry zone with low/high numbers.",
                "properties": {
                    "low": {"type": "number"},
                    "high": {"type": "number"},
                },
            },
            "waiting_trigger": {
                "type": "string",
                "description": "Plain-English condition being watched. For multi-step plans, describe the whole sequence but make trigger_rules monitor only the next unmet step.",
            },
            "trigger_rules": {
                "type": "array",
                "description": "Deterministic trigger rules. Each rule is an object with:\n"
                "  - type: 'price_breaks_level', 'price_crosses_level', 'candle_close', 'level_reclaim', 'level_breakdown', 'liquidity_sweep_touched', 'price_enters_zone', or 'price_approaches_zone'.\n"
                "  - side: 'above' or 'below' (required for level-based rules). Do NOT use 'condition' or 'direction' — use 'side'.\n"
                "  - level: a number (required for level-based rules and candle_close).\n"
                "  - low / high: numbers (for zone rules).\n"
                "  - timeframe: e.g. '15M', '4H' (optional, for candle_close).\n"
                "Use candle_close with side/level/timeframe for timeframe candle close alerts; "
                "use price_breaks_level when live intrabar breach is enough.",
                "items": {"type": "object"},
            },
            "invalidation_conditions": {
                "type": "array",
                "description": "Plain-English invalidation notes.",
                "items": {"type": "string"},
            },
            "invalidation_rules": {
                "type": "array",
                "description": "Deterministic invalidation rules. Same schema as trigger_rules (use 'side', not 'condition'; provide numeric 'level'). "
                "Use candle_close with side/level/timeframe for timeframe candle close invalidations; "
                "use price_breaks_level only when live intrabar breach is enough.",
                "items": {"type": "object"},
            },
            "analysis_context": {
                "type": "object",
                "description": "Optional extra context such as rating, grade, targets, setup_status, watcher_phase, thesis_continuity, thesis_summary, current_watcher_step, and next_condition_after_trigger.",
            },
            "watcher": {
                "type": "object",
                "description": "Optional full watcher payload. Top-level fields override this payload.",
            },
            "include_terminal": {
                "type": "boolean",
                "description": "Whether list includes terminal watchers.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum watcher rows to return.",
            },
        },
        "required": ["action"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "").strip().lower()
        watcher_id = str(kwargs.get("watcher_id") or "").strip()
        pair = str(kwargs.get("pair") or "").strip().upper()
        include_terminal = bool(kwargs.get("include_terminal", True))
        try:
            limit = max(1, min(500, int(kwargs.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100

        store = WatcherStore(default_watchers_dir())
        if action == "list":
            watchers = store.list(include_terminal=include_terminal, limit=limit)
            watchers = [item for item in watchers if item.status not in {WatchStatus.PAUSED, WatchStatus.CANCELLED}]
            if pair:
                watchers = [item for item in watchers if item.pair.strip().upper() == pair]
            return json.dumps(
                {
                    "status": "ok",
                    "action": action,
                    "watchers": [self._watcher_summary(item) for item in watchers],
                    "count": len(watchers),
                },
                ensure_ascii=False,
            )

        if action == "create":
            item, error = self._item_from_kwargs(kwargs)
            if error:
                return json.dumps({"status": "error", "action": action, "error": error}, ensure_ascii=False)
            assert item is not None
            duplicates = self._matching_active_watchers(store, item)
            if duplicates:
                primary = duplicates[0]
                self._copy_item_fields(item, primary)
                store.update(primary)
                deduped_ids: list[str] = []
                for duplicate in duplicates[1:]:
                    if store.delete(duplicate.id):
                        deduped_ids.append(duplicate.id)
                store.append_event(
                    WatcherEvent(
                        watcher_id=primary.id,
                        event_type="watcher.updated",
                        message="Existing watcher updated by AI tool create request.",
                        data={"deduped_watcher_ids": deduped_ids},
                    )
                )
                verified = store.get(primary.id)
                return json.dumps(
                    {
                        "status": "ok" if verified is not None else "error",
                        "action": action,
                        "watcher": self._watcher_summary(verified) if verified is not None else None,
                        "created": False,
                        "existing_reused": True,
                        "deduped_watcher_ids": deduped_ids,
                        "verified_created": verified is not None,
                        "verified_pollable": bool(verified.is_pollable) if verified is not None else False,
                    },
                    ensure_ascii=False,
                )
            triggered_duplicates = self._matching_triggered_watchers(store, item)
            if triggered_duplicates:
                return json.dumps(
                    {
                        "status": "error",
                        "action": action,
                        "error": (
                            "duplicate_triggered_watcher: this same watcher setup already triggered. "
                            "Do not recreate the same zone/rule. If analysis is VALID, do not create a watcher. "
                            "If analysis is still WAITING, update the fired watcher with a materially new deterministic trigger "
                            "for the next condition, e.g. a fresh price_breaks_level rule."
                        ),
                        "existing_watcher_ids": [existing.id for existing in triggered_duplicates],
                        "verified_created": False,
                        "verified_pollable": False,
                    },
                    ensure_ascii=False,
                )
            try:
                created = store.create(item)
                store.append_event(
                    WatcherEvent(
                        watcher_id=created.id,
                        event_type="watcher.created",
                        message="Watcher created by AI tool.",
                    )
                )
            except Exception as exc:
                return json.dumps({"status": "error", "action": action, "error": str(exc)}, ensure_ascii=False)
            verified = store.get(created.id)
            return json.dumps(
                {
                    "status": "ok" if verified is not None else "error",
                    "action": action,
                    "watcher": self._watcher_summary(verified) if verified is not None else None,
                    "verified_created": verified is not None,
                    "verified_pollable": bool(verified.is_pollable) if verified is not None else False,
                },
                ensure_ascii=False,
            )

        if not watcher_id:
            return json.dumps(
                {"status": "error", "action": action, "error": "watcher_id is required for this action"},
                ensure_ascii=False,
            )

        item = store.get(watcher_id)
        if action == "get":
            return json.dumps(
                {
                    "status": "ok" if item is not None else "not_found",
                    "action": action,
                    "watcher": self._watcher_summary(item) if item is not None else None,
                },
                ensure_ascii=False,
            )

        if item is None:
            return json.dumps(
                {"status": "not_found", "action": action, "watcher_id": watcher_id, "verified": True},
                ensure_ascii=False,
            )

        if action == "update":
            payload = self._merged_payload(kwargs)
            if isinstance(payload.get("trigger_rules"), list) and payload["trigger_rules"]:
                probe = WatchItem(
                    pair=str(payload.get("pair") or item.pair),
                    direction=str(payload.get("direction") or item.direction),
                    entry_zone=self._zone_from_payload(payload.get("entry_zone")) if "entry_zone" in payload else item.entry_zone,
                )
                if not self._normalize_trigger_rules(payload["trigger_rules"], probe):
                    detail = self._trigger_rules_error(
                        payload["trigger_rules"],
                        str(probe.pair or item.pair),
                        str(probe.direction or item.direction),
                    ) or (
                        "trigger_rules must use deterministic rule types "
                        "such as price_enters_zone, price_breaks_level, price_crosses_level, "
                        "level_reclaim, level_breakdown, candle_close, or liquidity_sweep_touched. "
                        "For level-based rules provide 'side' (\"above\"/\"below\") and 'level' (a number). "
                        "Do NOT use 'condition' — the evaluator only reads 'side'. "
                        "Put natural-language requirements in waiting_trigger/analysis_context."
                    )
                    return json.dumps(
                        {
                            "status": "error",
                            "action": action,
                            "watcher_id": watcher_id,
                            "error": f"unsupported_trigger_rules: {detail}",
                            "verified_updated": False,
                            "verified_pollable": False,
                        },
                        ensure_ascii=False,
                    )
            original_status = item.status
            if original_status == WatchStatus.TRIGGERED:
                requested_status = self._status_from_payload(payload) if "status" in payload else None
                if requested_status != WatchStatus.ACTIVE:
                    return json.dumps(
                        {
                            "status": "error",
                            "action": action,
                            "watcher_id": watcher_id,
                            "error": (
                                "triggered_watcher_not_pollable: this watcher already triggered and will not poll. "
                                "If the setup is VALID, do not update a watcher. If it is still WAITING, update status "
                                "to active with a materially new deterministic trigger rule for the next condition."
                            ),
                            "verified_updated": False,
                            "verified_pollable": False,
                        },
                        ensure_ascii=False,
                    )
            changed = self._apply_safe_updates(item, kwargs)
            if not changed:
                return json.dumps(
                    {
                        "status": "error",
                        "action": action,
                        "watcher_id": watcher_id,
                        "error": "no supported watcher fields were provided",
                    },
                    ensure_ascii=False,
                )
            if original_status == WatchStatus.TRIGGERED and item.status == WatchStatus.ACTIVE:
                fired_keys = set((item.watcher_state.fired_conditions or {}).keys())
                new_trigger_keys = set(self._trigger_condition_keys(item.trigger_rules))
                if fired_keys.intersection(new_trigger_keys):
                    return json.dumps(
                        {
                            "status": "error",
                            "action": action,
                            "watcher_id": watcher_id,
                            "error": (
                                "trigger_rule_already_fired: the requested active watcher uses the same trigger "
                                "condition that already fired. Use a new price_breaks_level or different zone for "
                                "the next WAITING condition, or leave the watcher triggered if analysis is VALID."
                            ),
                            "verified_updated": False,
                            "verified_pollable": False,
                        },
                        ensure_ascii=False,
                    )
                item.watcher_state = WatcherState(armed_at=utc_now_iso())
            store.update(item)
            store.append_event(
                WatcherEvent(
                    watcher_id=item.id,
                    event_type="watcher.updated",
                    message="Watcher updated by AI tool.",
                    data={"fields": changed},
                )
            )
            verified = store.get(watcher_id)
            return json.dumps(
                {
                    "status": "ok" if verified is not None else "error",
                    "action": action,
                    "watcher": self._watcher_summary(verified) if verified is not None else None,
                    "updated_fields": changed,
                    "verified_updated": verified is not None,
                    "verified_pollable": bool(verified.is_pollable) if verified is not None else False,
                },
                ensure_ascii=False,
            )

        if action == "delete":
            deleted = store.delete(watcher_id)
            verified_gone = store.get(watcher_id) is None
            return json.dumps(
                {
                    "status": "ok" if deleted and verified_gone else "error",
                    "action": action,
                    "watcher_id": watcher_id,
                    "deleted": bool(deleted),
                    "verified_gone": bool(verified_gone),
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {"status": "error", "action": action, "error": f"unsupported watcher action: {action}"},
            ensure_ascii=False,
        )

    @staticmethod
    def _watcher_summary(item: Any) -> dict[str, Any]:
        state = item.watcher_state
        return {
            "id": item.id,
            "pair": item.pair,
            "asset_class": item.asset_class,
            "status": item.status.value,
            "direction": item.direction,
            "timeframe": item.timeframe,
            "source": getattr(item, "source", "") or "",
            "entry_zone": item.entry_zone.to_dict() if item.entry_zone is not None else None,
            "waiting_trigger": item.waiting_trigger,
            "trigger_rules": item.trigger_rules,
            "invalidation_conditions": item.invalidation_conditions,
            "invalidation_rules": item.invalidation_rules,
            "analysis_context": dict(item.analysis_context or {}),
            "is_pollable": bool(item.is_pollable),
            "last_price": state.last_price,
            "last_checked": state.last_checked,
            "last_market_timestamp": state.last_market_timestamp,
            "last_event": state.last_event,
            "error_count": state.error_count,
            "last_error": state.last_error,
        }

    @classmethod
    def _item_from_kwargs(cls, kwargs: dict[str, Any]) -> tuple[WatchItem | None, str | None]:
        payload = cls._merged_payload(kwargs)
        pair = str(payload.get("pair") or "").strip().upper()
        if not pair:
            return None, "pair is required"
        direction = str(payload.get("direction") or "").strip().upper()
        if direction not in {"LONG", "SHORT"}:
            return None, "direction must be LONG or SHORT"
        trigger_rules = payload.get("trigger_rules")
        if trigger_rules is None:
            trigger_rules = []
        if not isinstance(trigger_rules, list):
            return None, "trigger_rules must be a list"
        invalidation_rules = payload.get("invalidation_rules")
        if invalidation_rules is None:
            invalidation_rules = []
        if not isinstance(invalidation_rules, list):
            return None, "invalidation_rules must be a list"
        invalidation_conditions = payload.get("invalidation_conditions")
        if invalidation_conditions is None:
            invalidation_conditions = []
        if not isinstance(invalidation_conditions, list):
            return None, "invalidation_conditions must be a list"
        context = payload.get("analysis_context") or {}
        if not isinstance(context, dict):
            return None, "analysis_context must be an object"
        context = cls._merge_analysis_context({}, context)
        payload["analysis_context"] = context
        item = WatchItem(
            status=cls._status_from_payload(payload),
            kind=WatchKind.WAITING_SETUP,
            pair=pair,
            asset_class=cls._asset_class_from_payload(payload),
            direction=direction,
            timeframe=str(payload.get("timeframe") or "").strip().upper(),
            source=cls._source_from_payload(payload),
            entry_zone=cls._zone_from_payload(payload.get("entry_zone")),
            waiting_trigger=str(payload.get("waiting_trigger") or "").strip(),
            trigger_rules=[],
            invalidation_conditions=[str(value) for value in invalidation_conditions],
            invalidation_rules=[],
            expiry=payload.get("expiry"),
            session_window=payload.get("session_window"),
            source_session_id=payload.get("source_session_id"),
            source_attempt_id=payload.get("source_attempt_id"),
            session_id=payload.get("session_id"),
            thread_id=payload.get("thread_id"),
            analysis_context=dict(context),
        )
        payload = cls._stage_multistep_pullback_payload(payload, item)
        item.entry_zone = cls._zone_from_payload(payload.get("entry_zone"))
        item.waiting_trigger = str(payload.get("waiting_trigger") or "").strip()
        item.analysis_context = dict(payload.get("analysis_context") or {})
        watcher_id = str(payload.get("watcher_id") or payload.get("id") or "").strip()
        if watcher_id:
            item.id = watcher_id
        trigger_rules = payload.get("trigger_rules")
        if trigger_rules is None:
            trigger_rules = []
        item.trigger_rules = cls._normalize_trigger_rules(trigger_rules, item)
        if trigger_rules and not item.trigger_rules:
            detail = cls._trigger_rules_error(trigger_rules, pair, direction) or ""
            return (
                None,
                (
                    "unsupported_trigger_rules: each trigger_rule must use a known type with "
                    "correct field names. "
                    + detail
                    if detail
                    else (
                        "unsupported_trigger_rules: trigger_rules must use deterministic rule types "
                        "such as price_enters_zone, price_breaks_level, price_crosses_level, "
                        "level_reclaim, level_breakdown, candle_close, or liquidity_sweep_touched. "
                        "For level-based rules (price_breaks_level, price_crosses_level, etc.) "
                        "provide 'side' (\"above\"/\"below\") and 'level' (a number). "
                        "Do NOT use 'condition' — the evaluator only reads 'side'. "
                        "Put natural-language requirements in waiting_trigger/analysis_context."
                    )
                ),
            )
        item.invalidation_rules = cls._normalize_invalidation_rules(invalidation_rules, item)
        guard_error = cls._creation_guard(item)
        if guard_error:
            return None, guard_error
        return item, None

    @classmethod
    def _creation_guard(cls, item: WatchItem) -> str | None:
        """Reject an incomplete watcher BEFORE it is persisted and polled.

        Runs AFTER normalization, so it sees the final structural shape the
        evaluator will read. A watcher that passes this guard is guaranteed to
        (a) name a pair the market-data layer can price, (b) declare a servable
        timeframe, and (c) carry at least one fully-specified deterministic
        rule with a human-readable condition. Every message names the offending
        field AND a concrete correction, so a retry succeeds on the next call
        instead of repeating the same malformed payload.

        This mirrors OpenAlice's ``watchSpecSchema`` superRefine block: the
        failure it prevents is a watcher that validates, persists, then fails
        every fetch forever while logging an error each tick and never firing.
        """
        # 1. Pair must be priceable. A source-prefixed symbol such as
        #    ``yfinance|SOLUSD`` (the exact mistake OpenAlice hit) is never a
        #    valid pair: the loader layer resolves the source itself.
        pair_error = cls._pair_error(item.pair, item.asset_class)
        if pair_error:
            return pair_error

        # 2. Timeframe must be one the loader layer can actually serve. An
        #    empty timeframe is allowed and defaults to daily; a wrong one such
        #    as ``2h`` or ``3d`` would poll-and-fail forever.
        raw_timeframe = str(item.timeframe or "").strip()
        if raw_timeframe:
            from src.watcher.market_data import normalize_timeframe

            normalized = normalize_timeframe(raw_timeframe)
            if normalized not in SUPPORTED_WATCH_INTERVALS:
                return (
                    f"unsupported_timeframe: {raw_timeframe!r} is not a servable bar interval. "
                    f"Use one of: {', '.join(SUPPORTED_WATCH_INTERVALS)} "
                    f"(or omit the timeframe to default to 1d)."
                )

        # 2b. Source must be a known loader venue. An unknown name would fail
        #     every fetch forever, so it is rejected at authoring time with the
        #     valid set. 'auto' is allowed and routes by market.
        source_error = cls._source_error(getattr(item, "source", ""))
        if source_error:
            return source_error

        # 3. At least one deterministic trigger rule must survive normalization.
        #    The normalizer auto-derives a ``price_enters_zone`` rule from the
        #    entry zone when no rules are supplied, so a watcher with a zone but
        #    no rules still passes; a watcher with neither is unpollable.
        if not item.trigger_rules:
            return (
                "missing_trigger_rule: a watcher needs at least one deterministic trigger rule "
                "(e.g. price_enters_zone with low/high, or price_breaks_level with side + level), "
                "or an entry_zone from which one can be derived. "
                "Natural-language requirements alone cannot be polled."
            )

        # 4. Every rule must be structurally complete. Normalization silently
        #    drops malformed rules, so a non-empty input that normalized to an
        #    incomplete rule set is caught here rather than polling a no-op.
        for index, rule in enumerate(item.trigger_rules):
            rule_error = cls._rule_completeness_error(index, rule)
            if rule_error:
                return rule_error

        # 5. A human-readable condition is required so the board card and the
        #    wake prompt can explain what is being watched without the rule table.
        if not str(item.waiting_trigger or "").strip():
            return (
                "missing_waiting_trigger: describe the condition in plain English "
                "(waiting_trigger), e.g. '15M candle closes below 79.00 then retest'."
            )

        return None

    @staticmethod
    def _pair_error(pair: str, asset_class: str) -> str | None:
        """Return a correction message when *pair* is not a priceable symbol."""
        clean = str(pair or "").strip()
        if not clean:
            return "missing_pair: a watcher needs a pair, e.g. BTCUSDT or GBPJPY."
        if "|" in clean or ":" in clean:
            return (
                f"invalid_pair: {clean!r} carries a source prefix. Pass the bare symbol "
                f"only (e.g. 'SOLUSDT' or 'GBPJPY'); the market-data layer picks the source."
            )
        if any(ch.isspace() for ch in clean):
            return f"invalid_pair: {clean!r} contains whitespace. Pass a single symbol token."
        if ".." in clean or "\\" in clean:
            return (
                f"invalid_pair: {clean!r} contains a path separator. Pass a symbol token, "
                f"e.g. 'GC=F' or 'AAPL.US'."
            )
        token = clean.upper()
        if not any(shape.match(token) for shape in _SYMBOL_SHAPES):
            return (
                f"invalid_pair: {clean!r} is not a symbol shape the market-data layer serves. "
                f"Accepted spellings: bare ticker (BTCUSDT, AAPL, XAUUSD), Yahoo continuous futures "
                f"(GC=F, CL=F), Yahoo FX (EURUSD=X, XAUUSD=X), Yahoo index (^SPX), exchange suffix "
                f"(AAPL.US, 0700.HK, VOD.L, EURUSD.FX) and crypto pair (BTC-USDT, BTC/USDT)."
            )
        return None

    @staticmethod
    def _source_error(source: str) -> str | None:
        """Return a correction message when *source* is not a known venue.

        ``""`` (omitted) and ``"auto"`` are always allowed: the former defaults
        to yfinance, the latter lets the registry route by market. Any other
        name must be a registered loader, because an unknown source would fail
        every fetch forever while never firing.
        """
        from src.watcher.market_data import normalize_source

        resolved = normalize_source(source)
        if resolved == "auto":
            return None
        try:
            from backtest.loaders.registry import VALID_SOURCES
        except Exception:
            return None  # Registry unavailable: do not block authoring.
        if resolved not in VALID_SOURCES:
            allowed = ", ".join(sorted(VALID_SOURCES))
            return (
                f"unknown_source: {resolved!r} is not a known market-data source. "
                f"Use 'auto' to route by market, or one of: {allowed}."
            )
        return None

    @staticmethod
    def _rule_completeness_error(index: int, rule: dict[str, Any]) -> str | None:
        """Return a correction message when a normalized rule lacks a numeric field."""
        rule_type = str(rule.get("type") or "").strip().lower()
        if rule_type in {"price_enters_zone", "price_approaches_zone"}:
            if rule.get("low") is None or rule.get("high") is None:
                return (
                    f"incomplete_trigger_rule: trigger_rules[{index}] ({rule_type}) has no low/high. "
                    f"Provide numeric 'low' and 'high'."
                )
            return None
        if rule.get("level") is None:
            return (
                f"incomplete_trigger_rule: trigger_rules[{index}] ({rule_type or 'unknown'}) has no 'level'. "
                f"Provide a numeric 'level' and a 'side' (\"above\"/\"below\")."
            )
        return None

    @classmethod
    def _apply_safe_updates(cls, item: WatchItem, kwargs: dict[str, Any]) -> list[str]:
        payload = cls._merged_payload(kwargs)
        payload = cls._stage_multistep_pullback_payload(payload, item)
        changed: list[str] = []

        def _set(name: str, value: Any) -> None:
            setattr(item, name, value)
            changed.append(name)

        if "pair" in payload and str(payload.get("pair") or "").strip():
            _set("pair", str(payload["pair"]).strip().upper())
        if "asset_class" in payload:
            _set("asset_class", cls._asset_class_from_payload(payload))
        if "direction" in payload and str(payload.get("direction") or "").strip():
            direction = str(payload["direction"]).strip().upper()
            if direction in {"LONG", "SHORT"}:
                _set("direction", direction)
        if "timeframe" in payload:
            _set("timeframe", str(payload.get("timeframe") or "").strip().upper())
        if "source" in payload:
            _set("source", cls._source_from_payload(payload))
        if "entry_zone" in payload:
            _set("entry_zone", cls._zone_from_payload(payload.get("entry_zone")))
        if "waiting_trigger" in payload:
            _set("waiting_trigger", str(payload.get("waiting_trigger") or "").strip())
        if isinstance(payload.get("trigger_rules"), list):
            _set("trigger_rules", cls._normalize_trigger_rules(payload["trigger_rules"], item))
        if isinstance(payload.get("invalidation_conditions"), list):
            _set("invalidation_conditions", [str(value) for value in payload["invalidation_conditions"]])
        if isinstance(payload.get("invalidation_rules"), list):
            _set("invalidation_rules", cls._normalize_invalidation_rules(payload["invalidation_rules"], item))
        if "analysis_context" in payload and isinstance(payload.get("analysis_context"), dict):
            _set("analysis_context", cls._merge_analysis_context(item.analysis_context or {}, payload["analysis_context"]))
        if "source_session_id" in payload and str(payload.get("source_session_id") or "").strip():
            _set("source_session_id", str(payload.get("source_session_id") or "").strip())
        if "source_attempt_id" in payload and str(payload.get("source_attempt_id") or "").strip():
            _set("source_attempt_id", str(payload.get("source_attempt_id") or "").strip())
        if "session_id" in payload and str(payload.get("session_id") or "").strip():
            _set("session_id", str(payload.get("session_id") or "").strip())
        if "thread_id" in payload and str(payload.get("thread_id") or "").strip():
            _set("thread_id", str(payload.get("thread_id") or "").strip())
        if "status" in payload:
            _set("status", cls._status_from_payload(payload))
        if any(
            field in changed
            for field in (
                "status",
                "entry_zone",
                "waiting_trigger",
                "trigger_rules",
                "invalidation_conditions",
                "invalidation_rules",
            )
        ):
            item.watcher_state.armed_at = utc_now_iso()
        return changed

    @staticmethod
    def _merged_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
        payload = dict(kwargs.get("watcher") or {}) if isinstance(kwargs.get("watcher"), dict) else {}
        for key, value in kwargs.items():
            if key in {"action", "watcher"}:
                continue
            if value is not None:
                payload[key] = value
        return payload

    @staticmethod
    def _merge_analysis_context(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing or {})
        update = dict(incoming or {})
        prior_phase = merged.get("watcher_phase")
        history = list(merged.get("thesis_history") or [])
        supplied_history = update.pop("thesis_history", None)
        if isinstance(supplied_history, list):
            history.extend(item for item in supplied_history if isinstance(item, dict))

        phase = update.get("watcher_phase")
        continuity = update.get("thesis_continuity")
        summary = update.get("thesis_summary") or update.get("setup_summary") or update.get("summary")
        waiting_for = update.get("waiting_for") or update.get("next_condition") or update.get("activation_trigger")
        should_record = any(value not in (None, "") for value in (phase, continuity, summary, waiting_for))
        if should_record:
            record = {
                "recorded_at": utc_now_iso(),
                "watcher_phase": phase,
                "previous_watcher_phase": update.get("previous_watcher_phase", prior_phase),
                "thesis_continuity": continuity,
                "thesis_summary": summary,
                "waiting_for": waiting_for,
            }
            record = {key: value for key, value in record.items() if value not in (None, "")}
            comparison_keys = ("watcher_phase", "thesis_continuity", "thesis_summary", "waiting_for")
            last = history[-1] if history and isinstance(history[-1], dict) else {}
            if any(record.get(key) != last.get(key) for key in comparison_keys):
                history.append(record)

        merged.update(update)
        if history:
            merged["thesis_history"] = history[-12:]
        return merged

    @classmethod
    def _stage_multistep_pullback_payload(cls, payload: dict[str, Any], item: WatchItem) -> dict[str, Any]:
        """Arm only the first unmet step for "retest zone, then confirm" plans.

        The watcher engine evaluates one deterministic trigger at a time. When the AI gives a
        full execution sequence, the safest persistence behavior is to poll the next step
        only and carry the later confirmation as context for the re-analysis handoff.
        """
        context = dict(payload.get("analysis_context") or {})
        phase = str(context.get("watcher_phase") or context.get("setup_status") or "").strip().upper()
        if phase == "THESIS_CONFIRMED_WAITING_ENTRY_CONFIRMATION":
            return payload

        waiting_trigger = str(
            payload.get("waiting_trigger")
            or context.get("waiting_for")
            or context.get("next_condition")
            or context.get("activation_trigger")
            or ""
        )
        if not cls._looks_like_multistep_pullback(waiting_trigger):
            return payload

        entry_zone = cls._zone_from_payload(payload.get("entry_zone")) or item.entry_zone or cls._zone_from_text(waiting_trigger)
        if entry_zone is None:
            return payload

        rules = payload.get("trigger_rules")
        if isinstance(rules, list) and any(cls._is_zone_rule(raw) for raw in rules if isinstance(raw, dict)):
            staged = dict(payload)
        else:
            staged = dict(payload)
            staged["trigger_rules"] = [
                {"type": "price_enters_zone", "low": entry_zone.low, "high": entry_zone.high}
            ]
        if "entry_zone" not in staged or staged.get("entry_zone") is None:
            staged["entry_zone"] = {"low": entry_zone.low, "high": entry_zone.high}

        next_condition = cls._next_condition_after_then(waiting_trigger)
        current_step = f"price enters/retests {entry_zone.low}-{entry_zone.high}"
        context.setdefault("watcher_phase", "THESIS_CONFIRMED_WAITING_ENTRY_PULLBACK")
        context["current_watcher_step"] = current_step
        context["next_condition_after_trigger"] = next_condition or "check rejection/CISD, then update this watcher to the final confirmation trigger"
        context["staged_watcher_sequence"] = True
        staged["analysis_context"] = context
        return staged

    @staticmethod
    def _looks_like_multistep_pullback(text: str) -> bool:
        clean = " ".join(str(text or "").lower().split())
        if "then" not in clean and "->" not in clean and "→" not in clean:
            return False
        has_pullback = re.search(r"\b(retest|retrace|retraces|retraced|pullback|pull back|return|returns|trade back|trades back|tap|taps)\b", clean)
        has_zone_language = re.search(r"\b(into|to|inside|zone|poi|fvg|ob|supply|demand|retest)\b", clean)
        has_later_confirmation = re.search(r"\b(close|closes|candle|reject|rejection|cisd|choch|displacement|breakdown|reclaim)\b", clean)
        return bool(has_pullback and has_zone_language and has_later_confirmation)

    @staticmethod
    def _next_condition_after_then(text: str) -> str:
        parts = re.split(r"\bthen\b|->|→", str(text or ""), maxsplit=1, flags=re.IGNORECASE)
        if len(parts) < 2:
            return ""
        return parts[1].strip(" .:-")

    @staticmethod
    def _zone_from_text(text: str) -> PriceZone | None:
        match = re.search(
            r"([0-9]+(?:\.[0-9]+)?)\s*(?:-|–|—|to)\s*([0-9]+(?:\.[0-9]+)?)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        try:
            left = float(match.group(1))
            right = float(match.group(2))
        except (TypeError, ValueError):
            return None
        low, high = sorted((left, right))
        return PriceZone(low=low, high=high)

    @staticmethod
    def _is_zone_rule(rule: dict[str, Any]) -> bool:
        return str(rule.get("type") or "").strip().lower() in {
            "zone",
            "entry_zone",
            "price_enters_zone",
            "price_approaches_zone",
        }

    @staticmethod
    def _zone_from_payload(value: Any) -> PriceZone | None:
        if not isinstance(value, dict):
            return None
        return PriceZone.from_dict(value)

    @staticmethod
    def _status_from_payload(payload: dict[str, Any]) -> WatchStatus:
        raw = str(payload.get("status") or "active").strip().lower()
        try:
            return WatchStatus(raw)
        except ValueError:
            return WatchStatus.ACTIVE

    @staticmethod
    def _asset_class_from_payload(payload: dict[str, Any]) -> str:
        raw = str(payload.get("asset_class") or "").strip().lower()
        if raw in {"crypto", "forex", "equity", "commodity", "index"}:
            return raw
        # Omitted: infer from the symbol rather than defaulting everything that
        # is not a 6-letter FX pair to crypto. GC=F is gold, not a coin.
        return infer_asset_class(str(payload.get("pair") or ""))

    @staticmethod
    def _source_from_payload(payload: dict[str, Any]) -> str:
        """Return the watcher's market-data source, defaulting to yfinance.

        The model picks a venue deliberately when its analysis used one; an
        omitted source lands on :data:`DEFAULT_WATCH_SOURCE` so the common case
        needs no extra field.
        """
        from src.watcher.market_data import normalize_source

        return normalize_source(payload.get("source"))

    @staticmethod
    def _trigger_rules_error(rules: list[Any], pair: str, direction: str) -> str | None:
        """Return a specific, actionable error message if *rules* have common
        field-name mistakes, or None if the rules look structurally sound.

        This is checked AFTER normalization — the normalizer silently drops
        rules with bad field names, and this method explains *why* so the AI
        can build a compliant payload on the next attempt.
        """
        for idx, raw in enumerate(rules):
            if not isinstance(raw, dict):
                continue
            rule = dict(raw)
            rule_type = str(rule.get("type") or "").strip().lower()

            if rule_type in {
                "price_breaks_level",
                "price_crosses_level",
                "level_reclaim",
                "level_breakdown",
                "liquidity_sweep_touched",
            }:
                level = rule.get("level") or rule.get("trigger_level")
                if level is None:
                    return (
                        f"trigger_rules[{idx}] (type={rule_type}) is missing 'level'. "
                        f"Add a numeric 'level', e.g. {{\"type\": \"{rule_type}\", \"side\": \"below\", \"level\": 79}}"
                    )
                try:
                    float(level)
                except (TypeError, ValueError):
                    return (
                        f"trigger_rules[{idx}] (type={rule_type}) has non-numeric 'level': {level!r}. "
                        f"Provide 'level' as a number."
                    )
                if "condition" in rule:
                    return (
                        f"trigger_rules[{idx}] (type={rule_type}) uses 'condition' (value={rule['condition']!r}) "
                        f"but the evaluator requires 'side' (\"above\"/\"below\"). "
                        f"Change {{\"condition\": {rule['condition']!r}}} to {{\"side\": \"{rule['condition']}\"}}."
                    )
                side = str(rule.get("side") or "").strip().lower()
                if side and side not in {"above", "below"}:
                    return (
                        f"trigger_rules[{idx}] (type={rule_type}) has invalid 'side': {side!r}. "
                        f"Use 'side': \"above\" or 'side': \"below\"."
                    )
            elif rule_type in {"zone", "entry_zone", "price_enters_zone", "price_approaches_zone"}:
                low = rule.get("low")
                high = rule.get("high")
                if low is None or high is None:
                    return (
                        f"trigger_rules[{idx}] (type={rule_type}) is missing 'low' and/or 'high'. "
                        f"Provide both, e.g. {{\"type\": \"{rule_type}\", \"low\": 124, \"high\": 126}}."
                    )
            elif rule_type == "candle_close":
                level = rule.get("level") or rule.get("trigger_level")
                if level is None:
                    return (
                        f"trigger_rules[{idx}] (type=candle_close) is missing 'level'. "
                        f"Add a numeric 'level'."
                    )
                try:
                    float(level)
                except (TypeError, ValueError):
                    return (
                        f"trigger_rules[{idx}] (type=candle_close) has non-numeric 'level': {level!r}."
                    )
            elif rule_type:
                return (
                    f"trigger_rules[{idx}] has unknown type: '{rule_type}'. "
                    f"Supported types: price_enters_zone, price_breaks_level, price_crosses_level, "
                    f"level_reclaim, level_breakdown, candle_close, liquidity_sweep_touched."
                )
        return None

    @classmethod
    def _normalize_trigger_rules(cls, rules: list[Any], item: WatchItem) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in rules:
            if not isinstance(raw, dict):
                continue
            rule = dict(raw)
            rule_type = str(rule.get("type") or "").strip().lower()
            if rule_type in {"zone", "entry_zone", "price_enters_zone", "price_approaches_zone"}:
                zone_rule = cls._zone_rule(rule, item)
                if zone_rule:
                    normalized.append(zone_rule)
                continue
            if rule_type == "candle_close":
                level = rule.get("level") or rule.get("trigger_level")
                if level is None:
                    continue
                side = str(rule.get("side") or "").strip().lower()
                if side not in {"above", "below"}:
                    side = "below" if item.direction == "SHORT" else "above"
                timeframe = str(rule.get("timeframe") or item.timeframe or "").strip()
                normalized_rule = {"type": "candle_close", "side": side, "level": float(level)}
                if timeframe:
                    normalized_rule["timeframe"] = timeframe
                normalized.append(normalized_rule)
                continue
            if rule_type in {"price_crosses_level", "level_reclaim", "level_breakdown", "price_breaks_level", "liquidity_sweep_touched"}:
                fixed = cls._normalize_side_level_rule(rule, item)
                if fixed:
                    normalized.append(fixed)
        if not normalized and not rules and item.entry_zone is not None:
            normalized.append({"type": "price_enters_zone", "low": item.entry_zone.low, "high": item.entry_zone.high})
        return normalized

    @staticmethod
    def _normalize_side_level_rule(rule: dict[str, Any], item: WatchItem) -> dict[str, Any] | None:
        """Validate a level-based rule and return a normalized copy, or None if invalid."""
        rule_type = str(rule.get("type") or "").strip().lower()

        if "condition" in rule or "dir" in rule:
            return None

        level = rule.get("level") or rule.get("trigger_level")
        if level is None:
            return None
        try:
            level = float(level)
        except (TypeError, ValueError):
            return None

        raw_side = str(rule.get("side") or "").strip().lower()
        if raw_side and raw_side not in {"above", "below"}:
            return None
        if raw_side in {"above", "below"}:
            side = raw_side
        else:
            raw_direction = str(rule.get("direction") or "").strip().lower()
            if raw_direction in {"above", "below"}:
                side = raw_direction
            elif rule_type == "level_reclaim":
                side = "above"
            elif rule_type == "level_breakdown":
                side = "below"
            elif item.direction.strip().upper() == "LONG":
                side = "above"
            elif item.direction.strip().upper() == "SHORT":
                side = "below"
            else:
                return None

        normalized_rule: dict[str, Any] = {"type": rule_type, "side": side, "level": level}
        if rule.get("timeframe"):
            normalized_rule["timeframe"] = str(rule["timeframe"]).strip()
        return normalized_rule

    @classmethod
    def _normalize_invalidation_rules(cls, rules: list[Any], item: WatchItem) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in rules:
            if not isinstance(raw, dict):
                continue
            rule = dict(raw)
            rule_type = str(rule.get("type") or "").strip().lower()
            if rule_type == "candle_close":
                level = rule.get("level") or rule.get("trigger_level")
                if level is None:
                    continue
                side = str(rule.get("side") or "").strip().lower()
                if side not in {"above", "below"}:
                    side = "above" if item.direction == "SHORT" else "below"
                timeframe = str(rule.get("timeframe") or item.timeframe or "").strip()
                normalized_rule = {"type": "candle_close", "side": side, "level": float(level)}
                if timeframe:
                    normalized_rule["timeframe"] = timeframe
                normalized.append(normalized_rule)
                continue
            if rule_type in {"price_crosses_level", "level_reclaim", "level_breakdown", "price_breaks_level"}:
                fixed = cls._normalize_side_level_rule(rule, item)
                if fixed:
                    normalized.append(fixed)
        return normalized

    @staticmethod
    def _zone_rule(rule: dict[str, Any], item: WatchItem) -> dict[str, Any] | None:
        low = rule.get("low")
        high = rule.get("high")
        if (low is None or high is None) and item.entry_zone is not None:
            low = item.entry_zone.low
            high = item.entry_zone.high
        if low is None or high is None:
            return None
        try:
            return {"type": "price_enters_zone", "low": float(low), "high": float(high)}
        except (TypeError, ValueError):
            return None

    @classmethod
    def _matching_active_watchers(cls, store: WatcherStore, item: WatchItem) -> list[WatchItem]:
        matches: list[WatchItem] = []
        for existing in store.list(include_terminal=False, limit=500):
            if existing.pair.strip().upper() != item.pair.strip().upper():
                continue
            if existing.direction.strip().upper() != item.direction.strip().upper():
                continue
            if existing.timeframe.strip().upper() != item.timeframe.strip().upper():
                continue
            if not cls._same_zone(existing.entry_zone, item.entry_zone):
                continue
            matches.append(existing)
        return matches

    @classmethod
    def _matching_triggered_watchers(cls, store: WatcherStore, item: WatchItem) -> list[WatchItem]:
        matches: list[WatchItem] = []
        for existing in store.list(include_terminal=True, limit=500):
            if existing.status != WatchStatus.TRIGGERED:
                continue
            if existing.pair.strip().upper() != item.pair.strip().upper():
                continue
            if existing.direction.strip().upper() != item.direction.strip().upper():
                continue
            if existing.timeframe.strip().upper() != item.timeframe.strip().upper():
                continue
            if not cls._same_zone(existing.entry_zone, item.entry_zone):
                continue
            if not cls._same_trigger_rules(existing.trigger_rules, item.trigger_rules):
                continue
            matches.append(existing)
        return matches

    @staticmethod
    def _same_zone(left: PriceZone | None, right: PriceZone | None) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return abs(left.low - right.low) < 0.00001 and abs(left.high - right.high) < 0.00001

    @classmethod
    def _same_trigger_rules(cls, left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
        return cls._canonical_trigger_rules(left) == cls._canonical_trigger_rules(right)

    @classmethod
    def _canonical_trigger_rules(cls, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        canonical = [cls._canonical_trigger_rule(rule) for rule in rules if isinstance(rule, dict)]
        return sorted(canonical, key=lambda entry: json.dumps(entry, sort_keys=True))

    @staticmethod
    def _canonical_trigger_rule(rule: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"type": str(rule.get("type") or "").strip().lower()}
        side = str(rule.get("side") or "").strip().lower()
        if side:
            out["side"] = side
        for key in ("low", "high", "level"):
            if key not in rule:
                continue
            try:
                out[key] = float(rule[key])
            except (TypeError, ValueError):
                out[key] = str(rule[key])
        return out

    @classmethod
    def _trigger_condition_keys(cls, rules: list[dict[str, Any]]) -> list[str]:
        keys: list[str] = []
        for index, rule in enumerate(rules):
            rule_type = str(rule.get("type") or "rule").strip()
            if rule.get("id"):
                keys.append(f"TRIGGER_HIT:{rule.get('id')}")
            elif rule_type in {"price_enters_zone", "price_approaches_zone"}:
                keys.append(f"TRIGGER_HIT:{rule_type}:{rule.get('low')}:{rule.get('high')}")
            elif "level" in rule:
                keys.append(f"TRIGGER_HIT:{rule_type}:{rule.get('side', '')}:{rule.get('level')}")
            else:
                keys.append(f"TRIGGER_HIT:{rule_type}:{index}")
        return keys

    @staticmethod
    def _copy_item_fields(source: WatchItem, target: WatchItem) -> None:
        target.status = WatchStatus.ACTIVE
        target.kind = WatchKind.WAITING_SETUP
        target.pair = source.pair
        target.asset_class = source.asset_class
        target.direction = source.direction
        target.timeframe = source.timeframe
        target.source = getattr(source, "source", "") or target.source
        target.entry_zone = source.entry_zone
        target.waiting_trigger = source.waiting_trigger
        target.trigger_rules = source.trigger_rules
        target.invalidation_conditions = source.invalidation_conditions
        target.invalidation_rules = source.invalidation_rules
        target.expiry = source.expiry
        target.session_window = source.session_window
        target.source_session_id = source.source_session_id or target.source_session_id
        target.source_attempt_id = source.source_attempt_id or target.source_attempt_id
        target.analysis_context = source.analysis_context

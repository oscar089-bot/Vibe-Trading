"""Shared runtime limits for tool results.

The cap on a tool result used to live in :mod:`src.agent.loop` and was copied as
a bare literal into :mod:`src.swarm.worker`, so the two could drift. It lives
here instead: a leaf module with no imports of its own, which the agent loop,
the swarm worker and individual tools can all read without any of them taking a
dependency on the others.
"""

from __future__ import annotations

#: Default maximum characters of a single tool result handed back to the model.
#: A tool may raise its own ceiling via ``BaseTool.result_limit`` when its
#: natural payload is legitimately larger (a full intraday OHLCV window);
#: ``resolve_result_limit`` is the single place that mapping is read.
TOOL_RESULT_LIMIT = 10_000

#: Delivery ceiling for ``get_market_data``. A full intraday OHLCV window
#: (250 rows, 4H/1H/15m) is ~23-47k compact characters — well past the shared
#: 10k default — so the default downsampled the series and the model computed
#: structure on a partial window without knowing it. The market-data tool
#: raises its own ceiling here so the complete window is delivered intact.
#: Context growth is still governed by the compaction layers; this widens the
#: per-result cut point, it does not remove the budget.
MARKET_DATA_RESULT_LIMIT = 48_000

#: Delivery ceiling for ``read_file``. The tool's own ``_OUTPUT_LIMIT`` caps a
#: single read at 50_000 characters; the shared 10_000 default cut a 13.4k
#: skill reference (smcpro/references/analysis.md, Step 5's ACTIVE volume
#: threshold) mid-sentence, because the JSON envelope wrapping the content
#: pushed it past the shared cap. The ceiling clears ``_OUTPUT_LIMIT`` plus the
#: envelope so a document the model explicitly asked to read arrives whole;
#: anything larger is paged via the tool's ``offset`` parameter.
READ_FILE_RESULT_LIMIT = 64_000


def resolve_result_limit(explicit: int | None) -> int:
    """Return the effective delivery cap for a tool.

    Args:
        explicit: A tool's ``result_limit`` (``None`` uses the shared default).

    Returns:
        The positive character cap to apply, or ``TOOL_RESULT_LIMIT`` when the
        tool declares none. A non-positive value is treated as unset so a
        misconfigured attribute can never cut every result to nothing.
    """
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    return TOOL_RESULT_LIMIT

_TRUNCATION_NOTICE = (
    "\n\n[TRUNCATED: {shown} of {total} characters delivered. The remainder was "
    "not sent. Do not treat this as the complete result — narrow the request, "
    "or call the tool again with its paging parameter if it has one.]"
)


def truncate_tool_result(result: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    """Cut an oversized tool result to ``limit`` characters and say so.

    A silent cut is indistinguishable from a complete answer, so the model
    reports a prefix as if it were the whole thing. Measured on live data,
    ``get_financial_statements("AAPL.US", period="quarter")`` returns 28,498
    characters for 40 periods; the silent cut delivered roughly 12 of them.

    Args:
        result: The full tool result.
        limit: Maximum characters to deliver, notice included.

    Returns:
        ``result`` unchanged when it fits, otherwise a prefix of exactly
        ``limit`` characters or fewer, ending in a notice that states how much
        was withheld.
    """
    total = len(result)
    if total <= limit:
        return result
    notice = _TRUNCATION_NOTICE.format(shown=limit, total=total)
    body = limit - len(notice)
    if body <= 0:
        # Pathologically small limit: the notice alone is more useful than a
        # fragment, so deliver it rather than silently cutting to nothing.
        return notice[:limit]
    return result[:body] + _TRUNCATION_NOTICE.format(shown=body, total=total)

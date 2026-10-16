"""Rich live turn-activity rendering for IM channels.

Mirrors the collapsed activity row a chat transport shows while an agent turn
runs: a braille spinner, a status header, the tools it is using, and one line
per FAILED tool. The renderer is pure — no I/O, no clock — so every transport
renders the SAME activity from the same structured input, and the tool-name
vocabulary is defined once instead of re-guessed per adapter.

Ported from OpenAlice's ``connector-protocol/turn-activity.ts`` so the two
surfaces agree on what a running turn looks like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# --- Tool vocabulary ---------------------------------------------------------

# Raw tool name -> present-participle phrase. A person watching a chat wants
# "Reading a file", not "multi_replace_string_in_file". Unknown names degrade to
# a readable de-snake form rather than being swallowed.
_TOOL_PHRASES: dict[str, str] = {
    # File + shell
    "read_file": "Reading a file",
    "read": "Reading a file",
    "write_file": "Writing a file",
    "write": "Writing a file",
    "edit_file": "Editing a file",
    "edit": "Editing a file",
    "multi_replace_string_in_file": "Editing files",
    "replace_string_in_file": "Editing a file",
    "create_file": "Creating a file",
    "list_dir": "Listing a directory",
    "file_search": "Searching files",
    "grep_search": "Searching the codebase",
    "bash": "Running a command",
    "shell": "Running a command",
    "run_in_terminal": "Running a command",
    # Research
    "fetch_webpage": "Reading a web page",
    "web_fetch": "Reading a web page",
    "web_search": "Searching the web",
    "search": "Searching",
    # Subagents / planning
    "runsubagent": "Running a sub-agent",
    "task": "Running a sub-agent",
    "manage_todo_list": "Updating the plan",
    "update_plan": "Updating the plan",
    # Trading surface
    "get_market_data": "Fetching market data",
    "market_data": "Fetching market data",
    "technical_indicators": "Computing indicators",
    "backtest": "Running a backtest",
    "factor_analysis": "Analysing factors",
    "options_pricing": "Pricing options",
    "symbol_search": "Looking up a symbol",
    "financial_statements": "Reading financials",
    "sec_filings": "Reading SEC filings",
    "trade_journal": "Analysing the trade journal",
    "run_swarm": "Running a swarm team",
}

# Raw tool name -> short label for the header meta (`bash` `read` `grep`).
_TOOL_LABELS: dict[str, str] = {
    "bash": "bash", "shell": "bash", "run_in_terminal": "bash", "command": "bash",
    "read": "read", "read_file": "read", "view": "read",
    "write": "write", "write_file": "write", "create_file": "write",
    "edit": "edit", "edit_file": "edit", "replace_string_in_file": "edit",
    "multi_replace_string_in_file": "edit",
    "grep_search": "grep", "grep": "grep", "file_search": "grep", "search": "grep",
    "list_dir": "ls", "ls": "ls",
    "fetch_webpage": "http", "web_fetch": "http", "web_search": "http",
    "runsubagent": "agent", "task": "agent",
    "manage_todo_list": "plan", "update_plan": "plan",
    "get_market_data": "market", "market_data": "market",
    "technical_indicators": "ta",
    "backtest": "backtest",
    "factor_analysis": "factor",
    "options_pricing": "options",
    "symbol_search": "symbol",
    "financial_statements": "fundamentals",
    "sec_filings": "sec",
    "run_swarm": "swarm",
}

# Braille spinner frames: one fixed-width cell in every monospace font, so the
# line never shifts horizontally as it animates.
_SPINNER_FRAMES = ("\u28fe", "\u28fd", "\u28fb", "\u28bf", "\u287f", "\u285f", "\u286f", "\u28f7")

_FAILED_MARK = "\u26a0\ufe0f"
_MAX_META_TOOLS = 3


def normalize_tool_key(name: str) -> str:
    """Lower-case a tool name and strip any ``mcp__server__`` prefix."""
    raw = (name or "").strip().lower()
    tail = raw.rsplit("__", 1)[-1] if "__" in raw else raw
    return tail.replace(" ", "_").replace("-", "_")


def humanize_tool_name(name: str) -> str:
    """``read_file`` -> ``Read file`` when no phrase is known."""
    words = [w for w in (name or "").replace("_", " ").replace("-", " ").split() if w]
    if not words:
        return "Working"
    return " ".join(w[:1].upper() + w[1:] for w in words)


def describe_tool_activity(name: str | None) -> str:
    """A short readable phrase for a running tool. Empty for a blank name."""
    if not name or not name.strip():
        return ""
    key = normalize_tool_key(name)
    return _TOOL_PHRASES.get(key) or humanize_tool_name(name)


def tool_label(name: str | None) -> str:
    """The short header-meta label for a tool. Empty for a blank name."""
    if not name or not name.strip():
        return ""
    key = normalize_tool_key(name)
    return _TOOL_LABELS.get(key) or key.split("_")[0]


def braille_spinner(frame: int) -> str:
    """Return the spinner glyph for ``frame`` (wraps around)."""
    return _SPINNER_FRAMES[abs(int(frame)) % len(_SPINNER_FRAMES)]


def format_tool_duration(duration_ms: float | None) -> str:
    """``420`` -> ``0.4s``; ``65000`` -> ``1m 5s``. Empty when unmeasured."""
    if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
        return ""
    if duration_ms < 100:
        return "<0.1s"
    if duration_ms < 1000:
        return f"{round(duration_ms / 100) / 10}s"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{round(seconds * 10) / 10:.1f}s"
    minutes = int(seconds // 60)
    rest = round(seconds % 60)
    return f"{minutes}m" if rest == 0 else f"{minutes}m {rest}s"


# --- Activity state ----------------------------------------------------------


@dataclass
class ToolRecord:
    """One tool invocation observed during a turn."""

    name: str
    status: str = "running"  # running | ok | failed
    duration_ms: float | None = None
    error: str = ""


@dataclass
class Activity:
    """Structured state of one running turn, as a transport renders it."""

    tool_calls: int = 0
    tool_failures: int = 0
    current_tool: str | None = None
    recent_tools: list[ToolRecord] = field(default_factory=list)
    narration: str = ""

    def note_call(self, name: str) -> None:
        """Record a tool starting; it becomes the current tool."""
        self.tool_calls += 1
        self.current_tool = name
        self.recent_tools.append(ToolRecord(name=name, status="running"))
        if len(self.recent_tools) > 12:
            del self.recent_tools[:-12]

    def note_result(self, name: str, *, ok: bool, duration_ms: float | None = None,
                    error: str = "") -> None:
        """Record a tool finishing; clear it as the current tool if it matches.

        A successful call also clears the accumulated failure lines. The
        failure COUNT stays in the header (``13 actions · 8 failed``), so the
        reader keeps the tally, but the per-tool detail lines are transient:
        once the turn moves on with a success, seven stale "Fetching market
        data" rows are noise, not information. This mirrors the reference
        renderer — failures are visible while the turn is stuck on them and
        collapse to the count once work resumes.
        """
        for record in reversed(self.recent_tools):
            if record.name == name and record.status == "running":
                record.status = "ok" if ok else "failed"
                record.duration_ms = duration_ms
                record.error = error if not ok else ""
                break
        if not ok:
            self.tool_failures += 1
        else:
            # A success means the turn is progressing; drop the now-stale
            # failure rows. The count is unaffected.
            self.recent_tools = [r for r in self.recent_tools if r.status != "failed"]
        if self.current_tool == name:
            self.current_tool = None

    def tool_meta(self) -> str:
        """``bash read grep`` — the newest distinct tool labels, capped."""
        labels: list[str] = []
        for record in self.recent_tools:
            label = tool_label(record.name)
            if label and label not in labels:
                labels.append(label)
        if self.current_tool:
            current = tool_label(self.current_tool)
            if current and current not in labels:
                labels.append(current)
        shown = labels[-_MAX_META_TOOLS:]
        if not shown:
            return ""
        meta = " ".join(f"`{label}`" for label in shown)
        return f"\u2026 {meta}" if len(labels) > _MAX_META_TOOLS else meta

    def counts_line(self) -> str:
        """``19 actions · 1 failed`` — empty before any tool has run."""
        if self.tool_calls <= 0:
            return ""
        calls = f"{self.tool_calls} action" if self.tool_calls == 1 else f"{self.tool_calls} actions"
        return f"{calls} \u00b7 {self.tool_failures} failed" if self.tool_failures else calls

    def failed_lines(self) -> list[str]:
        """One line per failed tool, oldest first."""
        lines = []
        for record in self.recent_tools:
            if record.status != "failed":
                continue
            label = describe_tool_activity(record.name) or record.name
            duration = format_tool_duration(record.duration_ms)
            timing = f" ({duration})" if duration else ""
            reason = f": {record.error}" if record.error else ""
            lines.append(f"{_FAILED_MARK} {label}{timing}{reason}")
        return lines


# --- Renderers ---------------------------------------------------------------


def _header(activity: Activity, frame: int, meta: str, emphasize: bool = False) -> str:
    spinner = braille_spinner(frame)
    counts = activity.counts_line()
    if not counts:
        return f"{spinner} \U0001f9e0 Thinking\u2026"
    status = "Working"
    if meta:
        return f"{spinner} {status} \u00b7 {counts} \u00b7 {meta}"
    return f"{spinner} {status} \u00b7 {counts}"


def render_activity(activity: Activity, frame: int) -> str:
    """Plain-text activity line (no markdown) for transports without rich HTML."""
    meta = activity.tool_meta().replace("`", "")
    lines = [_header(activity, frame, meta)]
    if activity.narration:
        lines.append(activity.narration)
    failures = activity.failed_lines()
    if failures:
        lines.append("\n".join(failures))
    return "\n".join(lines)


def _rich_meta_block(meta: str) -> str:
    """Render the tool-name meta as ``<code>`` chips for Telegram HTML.

    ``tool_meta()`` wraps each label in backticks for the markdown-ish plain
    variant; Telegram's HTML parse mode renders a backtick literally, so the
    chips are re-expressed as ``<code>`` to keep the monospace look.
    """
    from html import escape

    parts: list[str] = []
    for token in meta.split():
        if len(token) > 1 and token.startswith("`") and token.endswith("`"):
            parts.append(f"<code>{escape(token[1:-1])}</code>")
        elif token:
            parts.append(escape(token))
    return " ".join(parts)


def render_activity_rich(activity: Activity, frame: int) -> str:
    """Telegram-HTML activity card: bold header, blockquote meta, failures.

    The header stays one scannable line — ``Working · 19 actions · 1 failed`` —
    and the tool-name meta lives on its own blockquote beneath the narration,
    mirroring the desktop group's separate meta column. The meta must NOT also
    trail the header, or the same labels render twice on one crowded line.
    """
    from html import escape

    meta = activity.tool_meta()
    counts = activity.counts_line()
    spinner = braille_spinner(frame)
    if not counts:
        header = f"{spinner} \U0001f9e0 Thinking\u2026"
    else:
        header = f"{spinner} <b>Working</b> \u00b7 {escape(counts)}"
    lines = [header]
    if activity.narration:
        lines.append("")
        lines.append(escape(activity.narration))
    if meta:
        lines.append("")
        lines.append(f"<blockquote>{_rich_meta_block(meta)}</blockquote>")
    failures = activity.failed_lines()
    if failures:
        lines.append("")
        lines.append(escape("\n".join(failures)))
    return "\n".join(lines)


def render_tool_panel(tools: Sequence[ToolRecord] | None, *, expanded: int = 0) -> str:
    """A readable per-tool summary for logs/tests (not sent to chat)."""
    if not tools:
        return ""
    rows = []
    for record in tools:
        duration = format_tool_duration(record.duration_ms)
        mark = {"ok": "\u2713", "failed": _FAILED_MARK, "running": "\u22ef"}.get(record.status, "\u22ef")
        rows.append(f"{mark} {describe_tool_activity(record.name) or record.name}"
                    + (f" ({duration})" if duration else ""))
    return "\n".join(rows)

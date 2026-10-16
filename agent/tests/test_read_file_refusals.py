"""``read_file`` says where it may read, and lists a directory it is given.

Regression (2026-09-29, a live risk-parity run on DeepSeek V4): the model went
looking for the backtest engine's source — ``../../agent/src/backtest``, the
checkout's absolute path, ``skills/../backtest/engine.py`` — and every attempt
got "File not found or path escapes workspace". Each spelling was a new call,
so none was blocked, and none was an observation: eight rounds later the run
stopped with ``no_progress`` after 93 seconds, before any backtest ran. The
message said neither which of the two had happened nor where reading is
allowed, so the model kept guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.read_file_tool import ReadFileTool


@pytest.fixture()
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    run = tmp_path / "run"
    (run / "artifacts").mkdir(parents=True)
    (run / "artifacts" / "metrics.csv").write_text("sharpe\n0.69\n", encoding="utf-8")
    (run / "artifacts" / ".archived_backtest.json").write_text("{}", encoding="utf-8")
    return run


def _read(path: str, run: Path) -> dict:
    return json.loads(ReadFileTool().execute(path=path, run_dir=str(run)))


@pytest.mark.parametrize(
    "path",
    ["../../agent/src/backtest", "skills/../backtest/engine.py", "/etc/hosts"],
)
def test_a_path_outside_the_readable_roots_is_refused_as_such(run_dir: Path, path: str) -> None:
    body = _read(path, run_dir)

    assert body["status"] == "error"
    assert body["error_code"] == "outside_readable_roots"
    # The refusal carries the way forward, not just the verdict.
    assert "run directory" in body["error"] and "skills/" in body["error"]
    assert "load_skill" in body["error"]


def test_a_missing_file_inside_a_root_is_not_found(run_dir: Path) -> None:
    body = _read("artifacts/equity.csv", run_dir)

    assert body["error_code"] == "not_found"
    assert "load_skill" in body["error"]


def test_a_directory_is_answered_with_its_entries(run_dir: Path) -> None:
    listing = _read("artifacts", run_dir)
    skill = _read("skills/strategy-generate", run_dir)

    assert listing["status"] == "ok" and listing["kind"] == "directory"
    assert listing["entries"] == ["metrics.csv"]  # dotfiles stay out
    assert "SKILL.md" in skill["entries"]


def test_a_file_still_reads_as_before(run_dir: Path) -> None:
    body = _read("artifacts/metrics.csv", run_dir)

    assert body["status"] == "ok"
    assert body["content"] == "sharpe\n0.69\n"


def test_a_document_larger_than_the_shared_cap_is_delivered_whole(run_dir: Path) -> None:
    """A skill reference over the 10k tool-result cap must not be cut.

    Regression (2026-10-15): ``smcpro/references/analysis.md`` (13,421 chars)
    was cut mid-sentence at the ACTIVE volume threshold on two separate reads.
    The file was well under read_file's own 50k ``_OUTPUT_LIMIT`` — the cut was
    the agent loop's shared ``TOOL_RESULT_LIMIT = 10_000`` acting on the JSON
    envelope. read_file now declares its own ceiling so a document the model
    explicitly asked to read arrives whole, and pages anything larger.
    """
    from src.config.limits import TOOL_RESULT_LIMIT, resolve_result_limit, truncate_tool_result

    big = "x" * (TOOL_RESULT_LIMIT + 4_000)
    (run_dir / "artifacts" / "big.md").write_text(big, encoding="utf-8")
    body = _read("artifacts/big.md", run_dir)

    assert body["status"] == "ok"
    assert body["content"] == big, "a file under the read ceiling must arrive intact"
    # And the loop's cut, which was the real culprit, is a no-op for this tool.
    tool = ReadFileTool()
    assert resolve_result_limit(tool.result_limit) > TOOL_RESULT_LIMIT
    assert truncate_tool_result(json.dumps(body, ensure_ascii=False), resolve_result_limit(tool.result_limit)) == json.dumps(body, ensure_ascii=False)


def test_a_read_past_the_output_limit_reports_how_to_resume(run_dir: Path) -> None:
    """A read cut by ``_OUTPUT_LIMIT`` is self-describing, not a bare ellipsis.

    A truncated read that does not say how to continue is indistinguishable
    from a corrupt file. The envelope carries the total size, the amount
    returned, and the exact next ``offset``.
    """
    from src.tools import read_file_tool as mod

    big = "\n".join(f"line {i}" for i in range(1, 1_000))
    (run_dir / "artifacts" / "huge.md").write_text(big, encoding="utf-8")
    original = mod._OUTPUT_LIMIT
    mod._OUTPUT_LIMIT = 200
    try:
        body = _read("artifacts/huge.md", run_dir)
    finally:
        mod._OUTPUT_LIMIT = original

    assert body["status"] == "ok"
    assert body["truncated"] is True
    assert body["content_chars_total"] == len(big)
    assert body["content_chars_returned"] == len(body["content"])
    assert body["next_offset"] >= 1
    assert "offset=" in body["hint"]
    # Resuming from next_offset returns the lines after the cut, no split line.
    resumed = json.loads(
        ReadFileTool().execute(path="artifacts/huge.md", run_dir=str(run_dir), offset=body["next_offset"])
    )
    assert resumed["content"].startswith(f"line {body['next_offset'] + 1}")

"""Regression test for P08 (R1) — .env resolution must be observable.

A stale / shadowed .env silently won the config for the whole process with
zero diagnostic, costing hours. _ensure_dotenv now emits one behavior-
preserving INFO line naming the resolved slot via a redacted symbolic
label (or "none") plus the resolved provider/model/base. The absolute
path, OS username, and API key are never logged (CWE-209).
"""

from __future__ import annotations

import getpass
import logging
import os
from pathlib import Path

import pytest

import src.providers.llm as llm
from src.config.accessor import get_env_config, reset_env_config

LOGGER = "src.providers.llm"


@pytest.fixture
def fresh(monkeypatch):
    # Drop the once-per-process latch so the resolver actually runs.
    monkeypatch.setattr(llm, "_dotenv_loaded", False)


def test_logs_redacted_label_not_path(tmp_path, fresh, monkeypatch, caplog):
    """The resolved slot is logged as a symbolic label; the absolute path
    and OS username never appear (CWE-209)."""
    env = tmp_path / ".env"
    env.write_text("FOO=bar\n", encoding="utf-8")
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [env])
    monkeypatch.setattr(llm, "_ENV_LABELS", ("<TEST_SLOT>",))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        llm._ensure_dotenv()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "dotenv resolved from" in msg
    assert "<TEST_SLOT>" in msg  # redacted slot label is logged
    assert str(env) not in msg  # absolute path never logged
    assert str(tmp_path) not in msg
    assert getpass.getuser() not in msg  # OS username never leaks
    assert "sk-" not in msg  # key must never be logged


def test_redact_env_source_maps_real_candidates():
    """_redact_env_source maps fixed candidates to stable leak-free
    labels, None to the no-file sentinel, and any unknown path to a
    generic placeholder (never the real path)."""
    # The home slot never collides with AGENT_DIR / CWD -> exact mapping.
    assert llm._redact_env_source(llm._ENV_CANDIDATES[0]) == llm._ENV_LABELS[0]
    # Every fixed candidate resolves to a known redacted label. When CWD
    # == AGENT_DIR the earlier slot legitimately wins (matches the
    # first-match order of _ensure_dotenv) — still leak-free.
    for candidate in llm._ENV_CANDIDATES:
        result = llm._redact_env_source(candidate)
        assert result in llm._ENV_LABELS
        assert str(candidate) not in result
    assert llm._redact_env_source(None) == "none (no .env file found)"
    unknown = Path("/some/secret/home/user/.env")
    assert llm._redact_env_source(unknown) == "<.env>"
    assert str(unknown) not in llm._redact_env_source(unknown)


def test_logs_none_when_no_env_found(tmp_path, fresh, monkeypatch, caplog):
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [tmp_path / "does-not-exist.env"])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        llm._ensure_dotenv()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "none (no .env file found)" in msg


def test_loaded_dotenv_refreshes_cached_env_config(tmp_path, fresh, monkeypatch):
    """A config cached before dotenv loading must be refreshed afterwards."""
    env = tmp_path / ".env"
    env.write_text(
        "LANGCHAIN_PROVIDER=deepseek\n"
        "LANGCHAIN_MODEL_NAME=deepseek-chat\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [env])
    monkeypatch.setattr(llm, "_ENV_LABELS", ("<TEST_SLOT>",))
    monkeypatch.delenv("LANGCHAIN_PROVIDER", raising=False)
    monkeypatch.delenv("LANGCHAIN_MODEL_NAME", raising=False)

    reset_env_config()
    cached_before_load = get_env_config()
    assert cached_before_load.llm.langchain_provider == "openai"

    llm._ensure_dotenv()

    refreshed = get_env_config()
    assert refreshed is not cached_before_load
    assert refreshed.llm.langchain_provider == "deepseek"
    assert refreshed.llm.langchain_model_name == "deepseek-chat"


def test_logs_redacted_base_url_without_credentials(tmp_path, fresh, monkeypatch, caplog):
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [tmp_path / "does-not-exist.env"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://sk-secret@example.com:8443/v1?api_key=sk-hidden")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        llm._ensure_dotenv()
    msg = "\n".join(r.getMessage() for r in caplog.records)
    assert "base=https://example.com:8443" in msg
    assert "sk-secret" not in msg
    assert "sk-hidden" not in msg
    assert "api_key" not in msg


def test_latch_still_skips_second_call(tmp_path, fresh, monkeypatch, caplog):
    """Behavior preserved: still loads once per process (no log on re-entry)."""
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [tmp_path / "nope.env"])
    llm._ensure_dotenv()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        llm._ensure_dotenv()  # latched -> early return, no new log
    assert not [r for r in caplog.records if "dotenv resolved" in r.getMessage()]


# ── no-shadowing: every existing candidate is loaded ────────────────


def test_lower_precedence_candidate_is_not_shadowed(tmp_path, fresh, monkeypatch):
    """A key that lives ONLY in a lower-precedence .env must still load.

    Regression for a silent-shadowing trap: ``_ensure_dotenv`` used to stop at
    the first candidate that existed, so when ``~/.vibe-trading/.env`` was
    present (the Web UI writes it on any settings save) every key that lived
    only in ``agent/.env`` was unreachable. A flag such as
    ``VIBE_TRADING_ENABLE_WATCHER=1`` looked present on disk yet never reached
    ``os.environ``. On a VPS — where the home file is created by the first
    settings save and the operator cannot see the process's env — this is
    expensive to diagnose.
    """
    home = tmp_path / "home.env"
    agent = tmp_path / "agent.env"
    home.write_text("SHARED=from_home\nONLY_HOME=1\n", encoding="utf-8")
    agent.write_text("SHARED=from_agent\nONLY_AGENT=1\n", encoding="utf-8")
    for key in ("SHARED", "ONLY_HOME", "ONLY_AGENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [home, agent])

    llm._ensure_dotenv()

    # First file still wins for a duplicate key — precedence is unchanged.
    assert os.environ["SHARED"] == "from_home"
    # A key only the lower-precedence file defines now loads (the fix).
    assert os.environ["ONLY_AGENT"] == "1"
    assert os.environ["ONLY_HOME"] == "1"


def test_real_env_var_still_outranks_every_file(tmp_path, fresh, monkeypatch):
    """An exported variable keeps beating every .env, in any slot.

    ``override=False`` is load-bearing: an operator who exports a value in the
    process environment (or a docker-compose ``environment:`` block) must not
    have it silently replaced by a file.
    """
    home = tmp_path / "home.env"
    agent = tmp_path / "agent.env"
    home.write_text("PINNED=from_home\n", encoding="utf-8")
    agent.write_text("PINNED=from_agent\n", encoding="utf-8")
    monkeypatch.setenv("PINNED", "from_operator")
    monkeypatch.setattr(llm, "_ENV_CANDIDATES", [home, agent])

    llm._ensure_dotenv()

    assert os.environ["PINNED"] == "from_operator"

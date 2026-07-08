from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "resolve_session_resume.py"


def load_module():
    spec = importlib.util.spec_from_file_location("resolve_session_resume", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_module()


def write_rollout(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_resolve_resume_settings_prefers_turn_before_last_limit(module, tmp_path):
    codex_home = tmp_path / "codex-home"
    session_id = "11111111-1111-4111-8111-111111111111"
    rollout_path = codex_home / "sessions" / "2026" / "07" / "08" / f"rollout-2026-07-08T07-15-40-{session_id}.jsonl"
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-07T23:15:42.833Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4", "effort": "xhigh"},
            },
            {
                "timestamp": "2026-07-07T23:37:08.915Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4", "effort": "medium"},
            },
            {
                "timestamp": "2026-07-07T23:44:25.963Z",
                "type": "event_msg",
                "payload": {"rate_limits": {"primary": {"used_percent": 100.0}, "secondary": {"used_percent": 95.0}}},
            },
            {
                "timestamp": "2026-07-07T23:50:00.000Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4", "effort": "low"},
            },
        ],
    )

    assert module.resolve_resume_settings(session_id, codex_home=codex_home) == {
        "model": "gpt-5.4",
        "effort": "medium",
    }


def test_resolve_resume_settings_uses_latest_turn_without_limit(module, tmp_path):
    codex_home = tmp_path / "codex-home"
    session_id = "22222222-2222-4222-8222-222222222222"
    rollout_path = codex_home / "sessions" / "2026" / "07" / "08" / f"rollout-2026-07-08T06-44-28-{session_id}.jsonl"
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-07T23:15:42.833Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4-mini", "effort": "medium"},
            },
            {
                "timestamp": "2026-07-07T23:37:08.915Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4-mini", "effort": "high"},
            },
        ],
    )

    assert module.resolve_resume_settings(session_id, codex_home=codex_home) == {
        "model": "gpt-5.4-mini",
        "effort": "high",
    }


def test_resolve_resume_settings_falls_back_to_defaults_when_missing(module, tmp_path):
    codex_home = tmp_path / "codex-home"

    assert module.resolve_resume_settings("missing-session", codex_home=codex_home) == {
        "model": "gpt-5.4",
        "effort": "medium",
    }

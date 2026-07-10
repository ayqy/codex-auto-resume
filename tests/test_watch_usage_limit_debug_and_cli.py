from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import copy_fixture_tree, create_logs_db


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "watch_usage_limit.py"


def load_module():
    spec = importlib.util.spec_from_file_location("watch_usage_limit", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_module()


@pytest.fixture
def codex_home(tmp_path):
    return copy_fixture_tree("codex_home", tmp_path)


@pytest.fixture
def base_dir(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    return base


def seed_logs(path: Path):
    create_logs_db(
        path,
        [
            {
                "id": 1002,
                "ts": 1782345224,
                "level": "TRACE",
                "thread_id": None,
                "process_uuid": "pid:test:111",
                "feedback_log_body": (
                    'Received message {"type":"error","error":{"type":"usage_limit_reached","message":"The usage limit has been reached",'
                    '"plan_type":"plus","resets_at":1782353110,"eligible_promo":null,"resets_in_seconds":7886},'
                    '"status_code":429,"headers":{"X-Codex-Active-Limit":"premium","X-Codex-Plan-Type":"plus",'
                    '"X-Codex-Primary-Used-Percent":"100","X-Codex-Secondary-Used-Percent":"31",'
                    '"X-Codex-Primary-Window-Minutes":"300","X-Codex-Secondary-Window-Minutes":"10080",'
                    '"X-Codex-Primary-Reset-At":"1782353111","X-Codex-Secondary-Reset-At":"1782939911",'
                    '"X-Codex-Credits-Has-Credits":"False","X-Codex-Credits-Balance":"0","X-Codex-Credits-Unlimited":"False"}}'
                ),
            },
            {
                "id": 1003,
                "ts": 1782345224,
                "level": "INFO",
                "thread_id": "11111111-1111-4111-8111-111111111111",
                "process_uuid": "pid:test:111",
                "feedback_log_body": (
                    'session_loop{thread_id=11111111-1111-4111-8111-111111111111}: '
                    "run_turn: Turn error: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 10:05 AM."
                ),
            },
        ],
    )


def test_main_status_outputs_json(module, monkeypatch, base_dir, codex_home, capsys):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(module.sys, "argv", ["watch_usage_limit.py", "--status"])

    assert module.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert "pending_jobs" in data
    assert "prewarm_jobs" in data


def test_print_status_localizes_user_facing_timestamps(module, monkeypatch, base_dir, codex_home, capsys):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    watcher.state["thread_cache"] = {
        "session-local-time": {
            "id": "session-local-time",
            "rollout_path": None,
            "title": None,
            "cwd": None,
            "model_provider": None,
            "created_at": 1782345224,
            "updated_at": 1782348824,
            "source": "state_db",
        }
    }
    watcher.state["pending_jobs"] = [
        {
            "session_id": "session-local-time",
            "status": "pending",
            "scheduled_run_at": "2026-06-24T23:53:44Z",
        }
    ]

    assert watcher.print_status() == 0
    data = json.loads(capsys.readouterr().out)

    assert data["thread_cache"]["session-local-time"]["created_at"] == "2026-06-25T07:53:44+08:00"
    assert data["thread_cache"]["session-local-time"]["updated_at"] == "2026-06-25T08:53:44+08:00"
    assert data["pending_jobs"][0]["scheduled_run_at"] == "2026-06-25T07:53:44+08:00"


def test_main_debug_limit_history_outputs_sections(module, monkeypatch, base_dir, codex_home, capsys):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(module.sys, "argv", ["watch_usage_limit.py", "--debug-limit-history", "--days", "14"])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "Usage Limit Debug 近 14 天" in output
    assert "Confirmed Candidates" in output
    assert "Desired Pending Jobs" in output


def test_main_debug_session_outputs_session_json(module, monkeypatch, codex_home, capsys):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        module.sys,
        "argv",
        ["watch_usage_limit.py", "--debug-session", "11111111-1111-4111-8111-111111111111"],
    )

    assert module.main() == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert data["log_candidates_count"] >= 1


def test_debug_schedule_once_calls_run_once(module, monkeypatch, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    calls = []

    def fake_run_once(self):
        calls.append("run_once")
        return 0

    monkeypatch.setattr(module.UsageLimitWatcher, "run_once", fake_run_once)
    monkeypatch.setattr(module.sys, "argv", ["watch_usage_limit.py", "--debug-schedule-once"])

    assert module.main() == 0
    assert calls == ["run_once"]


def test_debug_force_latest_calls_force_latest(module, monkeypatch, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    calls = []

    def fake_force_latest(self):
        calls.append("force_latest")
        return 0

    monkeypatch.setattr(module.UsageLimitWatcher, "force_latest", fake_force_latest)
    monkeypatch.setattr(module.sys, "argv", ["watch_usage_limit.py", "--debug-force-latest"])

    assert module.main() == 0
    assert calls == ["force_latest"]


def test_sample_parse_logic_is_covered_as_unit_test(module, monkeypatch, base_dir, codex_home):
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    row = module.LogRow(
        id=1,
        ts=int(datetime(2026, 6, 25, 14, 0, tzinfo=module.ZoneInfo("UTC")).timestamp()),
        level="INFO",
        thread_id="sample-session",
        process_uuid="pid:test:sample",
        feedback_log_body=(
            "Turn error: You've hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at 3:13 PM."
        ),
    )
    retry_at, source = watcher.parse_retry_time([row], row)

    assert retry_at.hour == 15
    assert retry_at.minute == 13
    assert retry_at.date().isoformat() == "2026-06-26"
    assert source == "message"


def test_parse_retry_time_supports_long_date_message_format(module, monkeypatch, base_dir, codex_home):
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    row = module.LogRow(
        id=2,
        ts=int(datetime(2026, 7, 10, 12, 57, 44, tzinfo=module.ZoneInfo("UTC")).timestamp()),
        level="INFO",
        thread_id="sample-session",
        process_uuid="pid:test:sample",
        feedback_log_body=(
            "Turn error: You've hit your usage limit. Upgrade to Pro "
            "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at Jul 11th, 2026 12:59 AM."
        ),
    )

    retry_at, source = watcher.parse_retry_time([row], row)

    assert retry_at.isoformat() == "2026-07-11T00:59:00+08:00"
    assert source == "message"

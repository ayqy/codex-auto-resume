from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
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


def test_run_once_updates_state_and_triggers_due_jobs(module, monkeypatch, base_dir, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    now = datetime(2026, 7, 3, 6, 0, 0, tzinfo=watcher.local_tz)
    watcher.state["pending_jobs"] = [
        {
            "session_id": "22222222-2222-4222-8222-222222222222",
            "retry_at": "2026-07-02T05:05:11+08:00",
            "scheduled_run_at": "2026-07-02T05:15:11+08:00",
            "error_log_id": "rollout-old",
            "status": "pending",
            "cwd": "/workspace/secondary-project",
        }
    ]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            return datetime.fromtimestamp(ts, tz=tz)

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

        @classmethod
        def strptime(cls, date_string, fmt):
            return datetime.strptime(date_string, fmt)

    monkeypatch.setattr(module, "datetime", FakeDateTime)

    class Result:
        returncode = 0
        stderr = ""

    calls = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert watcher.run_once() == 0
    assert watcher.state["last_detected_session_id"] is not None
    assert watcher.state["triggered_jobs"]
    assert calls


def test_force_latest_success_and_failure(module, monkeypatch, base_dir, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)

    class Success:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Success())
    assert watcher.force_latest() == 0

    class Failure:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Failure())
    assert watcher.force_latest() == 1


def test_force_latest_no_candidate_returns_one(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)

    assert watcher.force_latest() == 1


def test_run_forever_uses_run_once_only(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    calls = []

    def fake_run_once():
        calls.append("run_once")
        raise KeyboardInterrupt()

    def fake_sleep(seconds):
        raise AssertionError("sleep should not be reached after KeyboardInterrupt")

    monkeypatch.setattr(watcher, "run_once", fake_run_once)
    monkeypatch.setattr(module.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        watcher.run_forever()

    assert calls == ["run_once"]

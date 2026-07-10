from __future__ import annotations

import importlib.util
import json
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


def write_config(base_dir: Path, workat: list[str]):
    (base_dir / "config.json").write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "",
                    "https": "",
                    "all": "",
                },
                "workat": workat,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
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


def test_run_once_executes_prewarm_jobs_without_usage_limit_events(module, monkeypatch, base_dir, codex_home):
    write_config(base_dir, ["10:30"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    now = datetime(2026, 7, 9, 6, 31, 0, tzinfo=watcher.local_tz)

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
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)
    monkeypatch.setattr(watcher, "build_desired_pending_jobs", lambda now=None: ({}, None, [], True, {}))

    class Result:
        returncode = 0
        stderr = ""

    calls = []

    def fake_run(command, capture_output, text, check, timeout=None):
        calls.append(command)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert watcher.run_once() == 0
    assert watcher.state["prewarm_jobs"]
    assert watcher.state["prewarm_jobs"][0]["status"] == "triggered"
    assert calls


def test_trigger_due_jobs_skips_session_that_already_has_agent_reply(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    rollout_path = (
        codex_home
        / "sessions"
        / "2026"
        / "06"
        / "25"
        / "rollout-2026-06-25T07-51-22-11111111-1111-4111-8111-111111111111.jsonl"
    )
    rollout_path.write_text(
        rollout_path.read_text(encoding="utf-8")
        + '\n{"timestamp":"2026-06-25T00:10:00.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"正常返回"}]}}\n',
        encoding="utf-8",
    )
    watcher.state["pending_jobs"] = [
        {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "retry_at": "2026-06-25T08:05:00+08:00",
            "scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "error_log_id": "limit-1",
            "status": "pending",
            "cwd": "/workspace/sample-app",
            "origin_event_at": "2026-06-25T07:53:44+08:00",
            "origin_retry_at": "2026-06-25T08:05:00+08:00",
            "origin_scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "governing_event_at": "2026-06-25T07:53:44+08:00",
            "governing_retry_at": "2026-06-25T08:05:00+08:00",
            "governing_scheduled_run_at": "2026-06-25T08:15:00+08:00",
        }
    ]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 6, 25, 8, 30, 0, tzinfo=watcher.local_tz)
            return current if tz else current.replace(tzinfo=None)

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

    calls = []

    def fake_run(command, capture_output, text, check, timeout=None):
        calls.append(command)
        raise AssertionError("manual-resumed session should not call terminal resume")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    summary = watcher.trigger_due_jobs()

    assert summary["attempted"] == 0
    assert summary["skipped_manual"] == 1
    assert watcher.state["pending_jobs"][0]["status"] == "expired"
    assert watcher.state["pending_jobs"][0]["status_reason"] == "session_already_resumed_manually"
    assert watcher.state["triggered_jobs"] == []
    assert calls == []


def test_run_once_cancels_future_pending_job_when_session_already_resumed(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    rollout_path = (
        codex_home
        / "sessions"
        / "2026"
        / "06"
        / "25"
        / "rollout-2026-06-25T07-51-22-11111111-1111-4111-8111-111111111111.jsonl"
    )
    rollout_path.write_text(
        rollout_path.read_text(encoding="utf-8")
        + '\n{"timestamp":"2026-06-25T00:10:00.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"正常返回"}]}}\n',
        encoding="utf-8",
    )
    watcher.state["pending_jobs"] = [
        {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "retry_at": "2026-06-25T08:05:00+08:00",
            "scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "error_log_id": "limit-1",
            "status": "pending",
            "cwd": "/workspace/sample-app",
            "origin_event_at": "2026-06-25T07:53:44+08:00",
            "origin_retry_at": "2026-06-25T08:05:00+08:00",
            "origin_scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "governing_event_at": "2026-06-25T07:53:44+08:00",
            "governing_retry_at": "2026-06-25T08:05:00+08:00",
            "governing_scheduled_run_at": "2026-06-25T08:15:00+08:00",
        }
    ]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 6, 25, 8, 12, 0, tzinfo=watcher.local_tz)
            return current if tz else current.replace(tzinfo=None)

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
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)

    candidate = watcher.normalize_candidate_metadata(
        {
            "source": "rollout",
            "priority": 2,
            "signal_strength": "rollout",
            "event_dt": datetime(2026, 6, 25, 7, 53, 44, tzinfo=watcher.local_tz),
            "error_id": "rollout:11111111-1111-4111-8111-111111111111:1:limit",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "retry_at": datetime(2026, 6, 25, 8, 5, 0, tzinfo=watcher.local_tz),
            "scheduled_run_at": datetime(2026, 6, 25, 8, 15, 0, tzinfo=watcher.local_tz),
            "thread_info": watcher.default_thread_info("11111111-1111-4111-8111-111111111111"),
            "message": "limit",
            "message_preview": "limit",
            "retry_source": "credits.primary.resets_at",
            "reason": "usage limit",
            "limit_kind": "rollout_primary_credits_exhausted",
            "primary_used_percent": 100.0,
            "secondary_used_percent": 31.0,
            "credits_has": False,
            "credits_balance": "0",
        }
    )
    monkeypatch.setattr(
        watcher,
        "collect_confirmed_candidates",
        lambda days=14, log_limit=5000, rollout_limit_threads=400: ([candidate], True),
    )

    assert watcher.run_once() == 0
    job = watcher.state["pending_jobs"][0]
    assert job["status"] == "expired"
    assert job["status_reason"] == "session_already_resumed_manually"


def test_run_once_only_cancels_resumed_pending_session(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    watcher.state["pending_jobs"] = [
        {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "retry_at": "2026-06-25T08:05:00+08:00",
            "scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "error_log_id": "limit-1",
            "status": "pending",
            "cwd": "/workspace/sample-app",
            "origin_event_at": "2026-06-25T07:53:44+08:00",
            "origin_retry_at": "2026-06-25T08:05:00+08:00",
            "origin_scheduled_run_at": "2026-06-25T08:15:00+08:00",
            "governing_event_at": "2026-06-25T07:53:44+08:00",
            "governing_retry_at": "2026-06-25T08:05:00+08:00",
            "governing_scheduled_run_at": "2026-06-25T08:15:00+08:00",
        },
        {
            "session_id": "22222222-2222-4222-8222-222222222222",
            "retry_at": "2026-06-25T08:06:00+08:00",
            "scheduled_run_at": "2026-06-25T08:16:00+08:00",
            "error_log_id": "limit-2",
            "status": "pending",
            "cwd": "/workspace/secondary-project",
            "origin_event_at": "2026-06-25T07:54:44+08:00",
            "origin_retry_at": "2026-06-25T08:06:00+08:00",
            "origin_scheduled_run_at": "2026-06-25T08:16:00+08:00",
            "governing_event_at": "2026-06-25T07:54:44+08:00",
            "governing_retry_at": "2026-06-25T08:06:00+08:00",
            "governing_scheduled_run_at": "2026-06-25T08:16:00+08:00",
        },
    ]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 6, 25, 8, 12, 0, tzinfo=watcher.local_tz)
            return current if tz else current.replace(tzinfo=None)

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
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)

    session_one = watcher.normalize_candidate_metadata(
        {
            "source": "rollout",
            "priority": 2,
            "signal_strength": "rollout",
            "event_dt": datetime(2026, 6, 25, 7, 53, 44, tzinfo=watcher.local_tz),
            "error_id": "rollout:11111111-1111-4111-8111-111111111111:1:limit",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "retry_at": datetime(2026, 6, 25, 8, 5, 0, tzinfo=watcher.local_tz),
            "scheduled_run_at": datetime(2026, 6, 25, 8, 15, 0, tzinfo=watcher.local_tz),
            "thread_info": watcher.default_thread_info("11111111-1111-4111-8111-111111111111"),
            "message": "limit",
            "message_preview": "limit",
            "retry_source": "credits.primary.resets_at",
            "reason": "usage limit",
            "limit_kind": "rollout_primary_credits_exhausted",
            "primary_used_percent": 100.0,
            "secondary_used_percent": 31.0,
            "credits_has": False,
            "credits_balance": "0",
        }
    )
    session_two = watcher.normalize_candidate_metadata(
        {
            "source": "rollout",
            "priority": 2,
            "signal_strength": "rollout",
            "event_dt": datetime(2026, 6, 25, 7, 54, 44, tzinfo=watcher.local_tz),
            "error_id": "rollout:22222222-2222-4222-8222-222222222222:1:limit",
            "session_id": "22222222-2222-4222-8222-222222222222",
            "retry_at": datetime(2026, 6, 25, 8, 6, 0, tzinfo=watcher.local_tz),
            "scheduled_run_at": datetime(2026, 6, 25, 8, 16, 0, tzinfo=watcher.local_tz),
            "thread_info": watcher.default_thread_info("22222222-2222-4222-8222-222222222222"),
            "message": "limit",
            "message_preview": "limit",
            "retry_source": "credits.primary.resets_at",
            "reason": "usage limit",
            "limit_kind": "rollout_primary_credits_exhausted",
            "primary_used_percent": 100.0,
            "secondary_used_percent": 31.0,
            "credits_has": False,
            "credits_balance": "0",
        }
    )
    monkeypatch.setattr(
        watcher,
        "collect_confirmed_candidates",
        lambda days=14, log_limit=5000, rollout_limit_threads=400: ([session_one, session_two], True),
    )
    monkeypatch.setattr(
        watcher,
        "session_has_normal_agent_activity_after",
        lambda session_id, after_dt: session_id == "11111111-1111-4111-8111-111111111111",
    )

    assert watcher.run_once() == 0

    resumed_jobs = [
        job
        for job in watcher.state["pending_jobs"]
        if job["session_id"] == "11111111-1111-4111-8111-111111111111"
    ]
    other_jobs = [
        job for job in watcher.state["pending_jobs"] if job["session_id"] == "22222222-2222-4222-8222-222222222222"
    ]

    assert resumed_jobs[0]["status"] == "expired"
    assert resumed_jobs[0]["status_reason"] == "session_already_resumed_manually"
    assert any(job["status"] == "pending" for job in other_jobs)


def test_run_once_without_user_visible_changes_prints_single_summary_line(
    module, monkeypatch, base_dir, codex_home, capsys
):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)
    monkeypatch.setattr(watcher, "build_desired_pending_jobs", lambda now=None: ({}, None, [], True, {}))

    assert watcher.run_once() == 0

    output_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(output_lines) == 1
    assert "ok pending=0 prewarm=0 next=-" in output_lines[0]
    assert "selected latest usage limit candidate" not in output_lines[0]
    assert "reconciling pending jobs" not in output_lines[0]
    assert "updated pending job metadata" not in output_lines[0]


def test_run_once_suppresses_due_prewarm_when_resume_runs(module, monkeypatch, base_dir, codex_home):
    write_config(base_dir, ["10:30"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    watcher = module.UsageLimitWatcher(base_dir, cleanup_on_init=False)
    now = datetime(2026, 7, 9, 6, 31, 0, tzinfo=watcher.local_tz)
    watcher.state["pending_jobs"] = [
        {
            "session_id": "22222222-2222-4222-8222-222222222222",
            "retry_at": "2026-07-09T06:20:00+08:00",
            "scheduled_run_at": "2026-07-09T06:30:00+08:00",
            "error_log_id": "limit-run-now",
            "status": "pending",
            "cwd": "/workspace/secondary-project",
            "origin_event_at": "2026-07-09T06:10:00+08:00",
            "origin_retry_at": "2026-07-09T06:20:00+08:00",
            "origin_scheduled_run_at": "2026-07-09T06:30:00+08:00",
            "governing_event_at": "2026-07-09T06:10:00+08:00",
            "governing_retry_at": "2026-07-09T06:20:00+08:00",
            "governing_scheduled_run_at": "2026-07-09T06:30:00+08:00",
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
    monkeypatch.setattr(watcher, "inspect_latest_error", lambda: None)
    monkeypatch.setattr(watcher, "build_desired_pending_jobs", lambda now=None: ({}, None, [], True, {}))

    class Result:
        returncode = 0
        stderr = ""

    resume_calls = []
    prewarm_calls = []

    def fake_run(command, capture_output, text, check, timeout=None):
        if len(command) >= 2 and str(command[1]).endswith("open_terminal_and_resume.py"):
            resume_calls.append(command)
            return Result()
        prewarm_calls.append(command)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert watcher.run_once() == 0
    assert resume_calls
    assert prewarm_calls == []
    assert watcher.state["prewarm_jobs"]
    assert watcher.state["prewarm_jobs"][0]["status"] == "expired"
    assert watcher.state["prewarm_jobs"][0]["status_reason"] == "suppressed_by_resume_priority"

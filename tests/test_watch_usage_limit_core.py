from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.helpers import copy_fixture_tree, create_logs_db, write_json


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
def base_dir(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    return base


@pytest.fixture
def codex_home(tmp_path):
    return copy_fixture_tree("codex_home", tmp_path)


def make_watcher(module, monkeypatch, base_dir, codex_home):
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return module.UsageLimitWatcher(base_dir, cleanup_on_init=False)


def seed_logs(path: Path):
    create_logs_db(
        path,
        [
            {
                "id": 1001,
                "ts": 1782345223,
                "level": "WARN",
                "thread_id": "11111111-1111-4111-8111-111111111111",
                "process_uuid": "pid:test:111",
                "feedback_log_body": (
                    'session_loop{thread_id=11111111-1111-4111-8111-111111111111}: '
                    "run_turn: Turn error: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 10:05 AM."
                ),
            },
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


def test_build_desired_pending_jobs_handles_global_override(module, monkeypatch, base_dir, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 6, 26, 12, 9, 0, tzinfo=watcher.local_tz)

    desired_jobs, global_candidate, active_candidates, logs_available = watcher.build_desired_pending_jobs(now=now, days=14)

    assert logs_available is True
    assert len(active_candidates) >= 1
    assert global_candidate is not None
    assert global_candidate["limit_scope"] == "global_window"
    assert "22222222-2222-4222-8222-222222222222" in desired_jobs
    job = desired_jobs["22222222-2222-4222-8222-222222222222"]
    assert job["governing_limit_scope"] == "global_window"
    assert job["governing_session_id"] == "22222222-2222-4222-8222-222222222222"


def test_reconcile_pending_jobs_marks_replaced_and_expired(module, monkeypatch, base_dir, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 6, 26, 12, 9, 0, tzinfo=watcher.local_tz)
    old_job = {
        "session_id": "22222222-2222-4222-8222-222222222222",
        "retry_at": "2026-06-26T11:30:17+08:00",
        "scheduled_run_at": "2026-06-26T11:40:17+08:00",
        "error_log_id": "old-error",
        "status": "pending",
    }
    expired_job = {
        "session_id": "expired-session",
        "retry_at": "2026-07-03T10:05:00+08:00",
        "scheduled_run_at": "2026-07-03T10:15:00+08:00",
        "error_log_id": "expired-error",
        "status": "pending",
    }
    watcher.state["pending_jobs"] = [old_job, expired_job]
    desired_jobs, _, _, _ = watcher.build_desired_pending_jobs(now=now, days=14)

    watcher.reconcile_pending_jobs(desired_jobs, now, allow_absent_prune=True)

    statuses = {job["session_id"]: job["status"] for job in watcher.state["pending_jobs"]}
    assert statuses["expired-session"] == "expired"
    assert any(job["status"] == "replaced" for job in watcher.state["pending_jobs"] if job["session_id"] == "22222222-2222-4222-8222-222222222222")
    assert any(job["status"] == "pending" and job["error_log_id"] != "old-error" for job in watcher.state["pending_jobs"] if job["session_id"] == "22222222-2222-4222-8222-222222222222")


def test_compute_sleep_seconds_for_due_and_future_jobs(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 6, 27, 12, 0, 0, tzinfo=watcher.local_tz)
    watcher.state["pending_jobs"] = [
        {"status": "pending", "scheduled_run_at": (now + timedelta(seconds=42)).isoformat()},
    ]
    assert watcher.compute_sleep_seconds(now=now) == 42.0

    watcher.state["pending_jobs"] = [
        {"status": "pending", "scheduled_run_at": (now - timedelta(seconds=1)).isoformat()},
    ]
    assert watcher.compute_sleep_seconds(now=now) == 1.0


def test_collect_confirmed_candidates_falls_back_when_logs_db_unavailable(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    broken_logs = codex_home / "logs_2.sqlite"
    broken_logs.write_text("not a sqlite db", encoding="utf-8")

    candidates, logs_available = watcher.collect_confirmed_candidates(days=14)

    assert logs_available is False
    assert any(candidate["source"] == "rollout" for candidate in candidates)

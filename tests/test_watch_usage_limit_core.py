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


def write_rollout(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def write_config(base_dir: Path, workat: list[str] | None):
    payload = {
        "proxy": {
            "http": "",
            "https": "",
            "all": "",
        }
    }
    if workat is not None:
        payload["workat"] = workat
    write_json(base_dir / "config.json", payload)


def test_build_desired_pending_jobs_handles_global_override(module, monkeypatch, base_dir, codex_home):
    seed_logs(codex_home / "logs_2.sqlite")
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 6, 26, 12, 9, 0, tzinfo=watcher.local_tz)

    desired_jobs, global_candidate, active_candidates, logs_available, prune_reasons = watcher.build_desired_pending_jobs(
        now=now, days=30
    )

    assert logs_available is True
    assert len(active_candidates) >= 1
    assert global_candidate is not None
    assert prune_reasons == {}
    assert global_candidate["limit_scope"] == "global_window"
    assert "22222222-2222-4222-8222-222222222222" in desired_jobs
    job = desired_jobs["22222222-2222-4222-8222-222222222222"]
    assert job["governing_limit_scope"] == "global_window"
    assert job["governing_session_id"] == "22222222-2222-4222-8222-222222222222"


def test_build_desired_pending_jobs_discards_invalidated_global_candidate(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 10, 3, 0, 0, tzinfo=watcher.local_tz)

    def make_candidate(
        session_id: str,
        error_id: str,
        event_dt: datetime,
        retry_at: datetime,
        retry_source: str,
        limit_kind: str,
        primary_used: float,
        secondary_used: float,
    ):
        return watcher.normalize_candidate_metadata(
            {
                "source": "rollout",
                "priority": 2,
                "signal_strength": "rollout",
                "event_dt": event_dt,
                "error_id": error_id,
                "session_id": session_id,
                "retry_at": retry_at,
                "scheduled_run_at": retry_at + timedelta(minutes=10),
                "thread_info": watcher.default_thread_info(session_id),
                "message": "limit",
                "message_preview": "limit",
                "retry_source": retry_source,
                "reason": "usage limit",
                "limit_kind": limit_kind,
                "primary_used_percent": primary_used,
                "secondary_used_percent": secondary_used,
                "credits_has": False,
                "credits_balance": "0",
            }
        )

    stale_global = make_candidate(
        session_id="stale-global-session",
        error_id="stale-global-error",
        event_dt=datetime(2026, 7, 9, 6, 54, 37, tzinfo=watcher.local_tz),
        retry_at=datetime(2026, 7, 13, 7, 56, 32, tzinfo=watcher.local_tz),
        retry_source="credits.secondary.resets_at",
        limit_kind="rollout_secondary_credits_exhausted",
        primary_used=52.0,
        secondary_used=100.0,
    )
    newer_session_limit = make_candidate(
        session_id="fresh-session",
        error_id="fresh-session-error",
        event_dt=datetime(2026, 7, 10, 2, 55, 0, tzinfo=watcher.local_tz),
        retry_at=datetime(2026, 7, 10, 3, 15, 56, tzinfo=watcher.local_tz),
        retry_source="credits.primary.resets_at",
        limit_kind="rollout_primary_credits_exhausted",
        primary_used=100.0,
        secondary_used=42.0,
    )

    monkeypatch.setattr(
        watcher,
        "collect_confirmed_candidates",
        lambda days=14, log_limit=5000, rollout_limit_threads=400: ([stale_global, newer_session_limit], True),
    )

    desired_jobs, global_candidate, active_candidates, logs_available, prune_reasons = watcher.build_desired_pending_jobs(
        now=now, days=14
    )

    assert logs_available is True
    assert global_candidate is None
    assert len(active_candidates) == 1
    assert prune_reasons == {}
    assert set(desired_jobs) == {"fresh-session"}
    assert desired_jobs["fresh-session"]["governing_limit_scope"] == "session_window"


def test_reconcile_pending_jobs_marks_replaced_and_preserves_future_pending_without_prune_reason(
    module, monkeypatch, base_dir, codex_home
):
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
    desired_jobs, _, _, _, prune_reasons = watcher.build_desired_pending_jobs(now=now, days=30)

    watcher.reconcile_pending_jobs(
        desired_jobs,
        now,
        allow_absent_prune=True,
        prune_reasons_by_session=prune_reasons,
    )

    statuses = {job["session_id"]: job["status"] for job in watcher.state["pending_jobs"]}
    assert statuses["expired-session"] == "pending"
    assert any(job["status"] == "replaced" for job in watcher.state["pending_jobs"] if job["session_id"] == "22222222-2222-4222-8222-222222222222")
    assert any(job["status"] == "pending" and job["error_log_id"] != "old-error" for job in watcher.state["pending_jobs"] if job["session_id"] == "22222222-2222-4222-8222-222222222222")


def test_choose_session_candidate_prefers_latest_event_over_later_scheduled_run_at(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    session_id = "latest-session"
    current = watcher.normalize_candidate_metadata(
        {
            "source": "rollout",
            "priority": 2,
            "signal_strength": "rollout",
            "event_dt": datetime(2026, 7, 10, 10, 57, 29, tzinfo=watcher.local_tz),
            "error_id": "rollout:latest-session:1:rollout_primary_limit",
            "session_id": session_id,
            "retry_at": datetime(2026, 7, 10, 11, 4, 48, tzinfo=watcher.local_tz),
            "scheduled_run_at": datetime(2026, 7, 10, 11, 14, 48, tzinfo=watcher.local_tz),
            "thread_info": watcher.default_thread_info(session_id),
            "message": "older rollout limit",
            "message_preview": "older rollout limit",
            "retry_source": "rate_limits.primary.resets_at",
            "reason": "rollout primary usage limit reached",
            "limit_kind": "rollout_primary_limit",
            "primary_used_percent": 100.0,
            "secondary_used_percent": 31.0,
            "credits_has": False,
            "credits_balance": "0",
        }
    )
    challenger = watcher.normalize_candidate_metadata(
        {
            "source": "logs",
            "priority": 1,
            "signal_strength": "strong",
            "event_dt": datetime(2026, 7, 10, 10, 58, 47, tzinfo=watcher.local_tz),
            "error_id": "57303522",
            "session_id": session_id,
            "retry_at": datetime(2026, 7, 10, 11, 4, 0, tzinfo=watcher.local_tz),
            "scheduled_run_at": datetime(2026, 7, 10, 11, 14, 0, tzinfo=watcher.local_tz),
            "thread_info": watcher.default_thread_info(session_id),
            "message": "newer explicit limit",
            "message_preview": "newer explicit limit",
            "retry_source": "message",
            "reason": "explicit usage limit turn error",
            "limit_kind": "log_turn_error",
            "primary_used_percent": 100.0,
            "secondary_used_percent": 31.0,
            "credits_has": False,
            "credits_balance": "0",
        }
    )

    assert watcher.choose_session_candidate(current, challenger) == challenger


def test_collect_rollout_candidates_for_thread_skips_transient_limit_when_normal_agent_message_follows(
    module, monkeypatch, base_dir, codex_home
):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    session_id = "transient-rollout-session"
    rollout_path = (
        codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T10-55-00-{session_id}.jsonl"
    )
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-10T02:55:00.000Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/workspace/transient",
                    "title": "Transient rollout session",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-07-10T02:57:29.073Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {"used_percent": 100.0, "resets_at": 1783652688},
                            "secondary": {"used_percent": 31.0, "resets_at": 1784239488},
                            "credits": {"has_credits": True, "balance": "12"},
                        },
                    },
                },
            {
                "timestamp": "2026-07-10T02:57:34.124Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "继续正常执行"}],
                },
            },
            {
                "timestamp": "2026-07-10T02:58:47.229Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": None},
            },
        ],
    )
    thread_info = watcher.parse_rollout_metadata(rollout_path, session_id=session_id)

    assert watcher.collect_rollout_candidates_for_thread(thread_info) == []


def test_collect_rollout_candidates_for_thread_keeps_terminal_limit_without_following_agent_message(
    module, monkeypatch, base_dir, codex_home
):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    session_id = "terminal-rollout-session"
    rollout_path = (
        codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T10-55-00-{session_id}.jsonl"
    )
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-10T02:55:00.000Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/workspace/terminal",
                    "title": "Terminal rollout session",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-07-10T02:57:29.073Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {"used_percent": 100.0, "resets_at": 1783652688},
                            "secondary": {"used_percent": 31.0, "resets_at": 1784239488},
                            "credits": {"has_credits": True, "balance": "12"},
                        },
                    },
                },
            {
                "timestamp": "2026-07-10T02:58:47.229Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": None},
            },
        ],
    )
    thread_info = watcher.parse_rollout_metadata(rollout_path, session_id=session_id)

    candidates = watcher.collect_rollout_candidates_for_thread(thread_info)

    assert len(candidates) == 1
    assert candidates[0]["source"] == "rollout"
    assert candidates[0]["reason"] == "rollout primary usage limit reached"


def test_collect_rollout_candidates_for_thread_detects_terminal_premium_credits_exhausted_from_previous_primary_reset(
    module, monkeypatch, base_dir, codex_home
):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    session_id = "terminal-premium-session"
    rollout_path = (
        codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T20-03-02-{session_id}.jsonl"
    )
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-10T12:03:02.125Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "cwd": "/workspace/premium-terminal",
                    "title": "Terminal premium session",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-07-10T12:31:44.365Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 80.0, "window_minutes": 300, "resets_at": 1783702749},
                        "secondary": {"used_percent": 44.0, "window_minutes": 10080, "resets_at": 1784239488},
                        "credits": None,
                        "individual_limit": None,
                        "plan_type": "plus",
                        "rate_limit_reached_type": None,
                    },
                },
            },
            {
                "timestamp": "2026-07-10T12:37:40.342Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "premium",
                        "primary": None,
                        "secondary": None,
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                        "individual_limit": None,
                        "plan_type": "plus",
                        "rate_limit_reached_type": None,
                    },
                },
            },
            {
                "timestamp": "2026-07-10T12:37:40.347Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": None},
            },
        ],
    )
    thread_info = watcher.parse_rollout_metadata(rollout_path, session_id=session_id)

    candidates = watcher.collect_rollout_candidates_for_thread(thread_info)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["source"] == "rollout"
    assert candidate["limit_kind"] == "rollout_premium_credits_exhausted"
    assert candidate["retry_source"] == "inferred.previous_primary_reset_at"
    assert candidate["reason"] == "rollout premium credits exhausted inferred from previous primary reset"
    assert candidate["candidate_family"] == "session_credits_exhausted"
    assert candidate["limit_scope"] == "session_window"
    assert candidate["retry_at"].isoformat() == "2026-07-11T00:59:09+08:00"
    assert candidate["scheduled_run_at"].isoformat() == "2026-07-11T01:09:09+08:00"


def test_collect_rollout_candidates_for_thread_skips_transient_premium_credits_exhausted_when_normal_agent_message_follows(
    module, monkeypatch, base_dir, codex_home
):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    session_id = "transient-premium-session"
    rollout_path = (
        codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T20-03-02-{session_id}.jsonl"
    )
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-10T12:03:02.125Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "cwd": "/workspace/premium-transient",
                    "title": "Transient premium session",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-07-10T12:31:44.365Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 80.0, "window_minutes": 300, "resets_at": 1783702749},
                        "secondary": {"used_percent": 44.0, "window_minutes": 10080, "resets_at": 1784239488},
                        "credits": None,
                        "individual_limit": None,
                        "plan_type": "plus",
                        "rate_limit_reached_type": None,
                    },
                },
            },
            {
                "timestamp": "2026-07-10T12:37:40.342Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "premium",
                        "primary": None,
                        "secondary": None,
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                        "individual_limit": None,
                        "plan_type": "plus",
                        "rate_limit_reached_type": None,
                    },
                },
            },
            {
                "timestamp": "2026-07-10T12:37:40.345Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "继续正常执行"}],
                },
            },
            {
                "timestamp": "2026-07-10T12:37:40.347Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": None},
            },
        ],
    )
    thread_info = watcher.parse_rollout_metadata(rollout_path, session_id=session_id)

    assert watcher.collect_rollout_candidates_for_thread(thread_info) == []


def test_build_desired_pending_jobs_prefers_latest_log_error_after_transient_rollout_limit(
    module, monkeypatch, base_dir, codex_home
):
    session_id = "019f49c4-e8b6-7523-a3fa-45312102d488"
    create_logs_db(
        codex_home / "logs_2.sqlite",
        [
            {
                "id": 57303522,
                "ts": int(datetime(2026, 7, 10, 10, 58, 47, tzinfo=module.ZoneInfo("Asia/Shanghai")).timestamp()),
                "level": "INFO",
                "thread_id": session_id,
                "process_uuid": "pid:test:transient",
                "feedback_log_body": (
                    f'session_loop{{thread_id={session_id}}}: '
                    "run_turn: Turn error: You've hit your usage limit. Upgrade to Pro "
                    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                    "to purchase more credits or try again at 11:04 AM."
                ),
            }
        ],
    )
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    rollout_path = (
        codex_home / "sessions" / "2026" / "07" / "10" / f"rollout-2026-07-10T10-04-29-{session_id}.jsonl"
    )
    write_rollout(
        rollout_path,
        [
            {
                "timestamp": "2026-07-10T02:55:00.000Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/workspace/transient",
                    "title": "Transient session",
                    "model_provider": "openai",
                },
            },
            {
                "timestamp": "2026-07-10T02:57:29.073Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                        "rate_limits": {
                            "limit_id": "codex",
                            "primary": {"used_percent": 100.0, "resets_at": 1783652688},
                            "secondary": {"used_percent": 31.0, "resets_at": 1784239488},
                            "credits": {"has_credits": True, "balance": "12"},
                        },
                    },
                },
            {
                "timestamp": "2026-07-10T02:57:34.124Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "继续正常执行"}],
                },
            },
            {
                "timestamp": "2026-07-10T02:58:47.229Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": None},
            },
        ],
    )
    record = {"session_id": session_id, "rollout_path": str(rollout_path), "mtime": rollout_path.stat().st_mtime}
    monkeypatch.setattr(watcher, "scan_rollout_index", lambda force=False: {"by_session": {session_id: record}, "recent": [record]})

    desired_jobs, _, _, _, prune_reasons = watcher.build_desired_pending_jobs(
        now=datetime(2026, 7, 10, 11, 0, 0, tzinfo=watcher.local_tz),
        days=14,
    )

    assert prune_reasons == {}
    assert session_id in desired_jobs
    assert desired_jobs[session_id]["origin_source"] == "logs"
    assert desired_jobs[session_id]["origin_error_id"] == "57303522"
    assert desired_jobs[session_id]["scheduled_run_at"] == "2026-07-10T11:14:00+08:00"


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

    candidates, logs_available = watcher.collect_confirmed_candidates(days=30)

    assert logs_available is False
    assert any(candidate["source"] == "rollout" for candidate in candidates)


def test_thread_exists_state_db_fallback_is_file_only(module, monkeypatch, base_dir, codex_home, capsys):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    watcher.state_db.write_text("not a sqlite db", encoding="utf-8")

    thread_info = watcher.thread_exists("11111111-1111-4111-8111-111111111111")

    assert capsys.readouterr().out == ""
    assert thread_info["id"] == "11111111-1111-4111-8111-111111111111"
    assert "fallback to rollout/cache" in watcher.log_path.read_text(encoding="utf-8")


def test_build_desired_prewarm_jobs_skips_when_workat_not_configured(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 6, 0, 0, tzinfo=watcher.local_tz)

    desired_jobs = watcher.build_desired_prewarm_jobs(now=now)

    assert desired_jobs == {}


def test_build_desired_prewarm_jobs_generates_today_and_tomorrow(module, monkeypatch, base_dir, codex_home):
    write_config(base_dir, ["14:00", "10:30"])
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 6, 0, 0, tzinfo=watcher.local_tz)

    desired_jobs = watcher.build_desired_prewarm_jobs(now=now)

    assert sorted(desired_jobs) == [
        "2026-07-09|10:30",
        "2026-07-09|14:00",
        "2026-07-10|10:30",
        "2026-07-10|14:00",
    ]
    assert desired_jobs["2026-07-09|10:30"]["scheduled_run_at"] == "2026-07-09T06:30:00+08:00"
    assert desired_jobs["2026-07-09|10:30"]["expected_reset_at"] == "2026-07-09T11:30:00+08:00"
    assert desired_jobs["2026-07-09|10:30"]["model"] == "gpt-5.4-mini"
    assert desired_jobs["2026-07-09|10:30"]["effort"] == "low"
    assert desired_jobs["2026-07-09|10:30"]["prompt_preview"] == "Just say Hi"


def test_reconcile_prewarm_jobs_expires_old_schedule_and_adds_new_job(module, monkeypatch, base_dir, codex_home):
    write_config(base_dir, ["14:00"])
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 8, 0, 0, tzinfo=watcher.local_tz)
    watcher.state["prewarm_jobs"] = [
        {
            "job_key": "2026-07-09|10:30",
            "workat": "10:30",
            "workat_at": "2026-07-09T10:30:00+08:00",
            "scheduled_run_at": "2026-07-09T06:30:00+08:00",
            "run_deadline_at": "2026-07-09T06:35:00+08:00",
            "expected_reset_at": "2026-07-09T11:30:00+08:00",
            "model": "gpt-5.4-mini",
            "effort": "low",
            "prompt_preview": "Just say Hi",
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    ]

    desired_jobs = watcher.build_desired_prewarm_jobs(now=now)
    watcher.reconcile_prewarm_jobs(desired_jobs, now)

    statuses = {job["job_key"]: job["status"] for job in watcher.state["prewarm_jobs"]}
    assert statuses["2026-07-09|10:30"] == "expired"
    assert statuses["2026-07-09|14:00"] == "pending"


def test_trigger_due_prewarm_jobs_marks_late_job_expired(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 6, 36, 0, tzinfo=watcher.local_tz)
    watcher.state["prewarm_jobs"] = [
        {
            "job_key": "2026-07-09|10:30",
            "workat": "10:30",
            "workat_at": "2026-07-09T10:30:00+08:00",
            "scheduled_run_at": "2026-07-09T06:30:00+08:00",
            "run_deadline_at": "2026-07-09T06:35:00+08:00",
            "expected_reset_at": "2026-07-09T11:30:00+08:00",
            "model": "gpt-5.4-mini",
            "effort": "low",
            "prompt_preview": "Just say Hi",
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    ]

    watcher.trigger_due_prewarm_jobs(now=now)

    job = watcher.state["prewarm_jobs"][0]
    assert job["status"] == "expired"
    assert job["status_reason"] == "missed_prewarm_deadline"


def test_trigger_due_prewarm_jobs_is_suppressed_when_resume_has_priority(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 6, 31, 0, tzinfo=watcher.local_tz)
    watcher.state["prewarm_jobs"] = [
        {
            "job_key": "2026-07-09|10:30",
            "workat": "10:30",
            "workat_at": "2026-07-09T10:30:00+08:00",
            "scheduled_run_at": "2026-07-09T06:30:00+08:00",
            "run_deadline_at": "2026-07-09T06:35:00+08:00",
            "expected_reset_at": "2026-07-09T11:30:00+08:00",
            "model": "gpt-5.4-mini",
            "effort": "low",
            "prompt_preview": "Just say Hi",
            "status": "pending",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    ]

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("prewarm command should not run when auto resume has priority")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    watcher.trigger_due_prewarm_jobs(now=now, suppress_for_resume=True)

    job = watcher.state["prewarm_jobs"][0]
    assert job["status"] == "expired"
    assert job["status_reason"] == "suppressed_by_resume_priority"
    assert calls == []


def test_compute_sleep_seconds_prefers_nearest_prewarm_job(module, monkeypatch, base_dir, codex_home):
    watcher = make_watcher(module, monkeypatch, base_dir, codex_home)
    now = datetime(2026, 7, 9, 6, 0, 0, tzinfo=watcher.local_tz)
    watcher.state["pending_jobs"] = [
        {"status": "pending", "scheduled_run_at": (now + timedelta(seconds=90)).isoformat()},
    ]
    watcher.state["prewarm_jobs"] = [
        {"status": "pending", "scheduled_run_at": (now + timedelta(seconds=42)).isoformat()},
    ]

    assert watcher.compute_sleep_seconds(now=now) == 42.0

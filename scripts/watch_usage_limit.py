#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TIME_RE = re.compile(r"try again at ([0-9]{1,2}:[0-9]{2} [AP]M)")
RESETS_AT_RE = re.compile(r'"resets_at":\s*(\d+)')
PRIMARY_RESET_AT_HEADER_RE = re.compile(r'"X-Codex-Primary-Reset-At":"(\d+)"')
SESSION_ID_RE = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$")


@dataclass
class LogRow:
    id: int
    ts: int
    level: str
    thread_id: Optional[str]
    process_uuid: Optional[str]
    feedback_log_body: str


class UsageLimitWatcher:
    def __init__(self, base_dir: Path, cleanup_on_init: bool = True):
        self.base_dir = base_dir
        self.codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.logs_db = self.codex_home / "logs_2.sqlite"
        self.state_db = self.codex_home / "state_5.sqlite"
        self.sessions_root = self.codex_home / "sessions"
        self.tmp_dir = self.base_dir / "tmp"
        self.logs_dir = self.tmp_dir / "logs"
        self.state_path = self.tmp_dir / "state.json"
        self.log_path = self.logs_dir / "watcher.log"
        self.local_tz = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
        self.poll_interval = 1800
        self.rollout_index_cache = None
        self.rollout_index_built_at = 0.0
        self.state_db_warning_contexts = set()
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.state = self.load_state()
        if cleanup_on_init:
            self.cleanup_state()

    def default_state(self):
        return {
            "last_seen_log_id": 0,
            "processed_error_keys": [],
            "pending_jobs": [],
            "triggered_jobs": [],
            "last_detected_error": None,
            "last_detected_session_id": None,
            "last_detected_retry_at": None,
            "last_detected_scheduled_run_at": None,
            "last_detected_cwd": None,
            "thread_cache": {},
        }

    def load_state(self):
        if not self.state_path.exists():
            state = self.default_state()
            self.save_state(state)
            return state
        loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        state = self.default_state()
        state.update(loaded)
        if not isinstance(state.get("thread_cache"), dict):
            state["thread_cache"] = {}
        return state

    def save_state(self, state=None):
        if state is not None:
            self.state = state
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="state.", suffix=".json", dir=str(self.tmp_dir))
        os.close(tmp_fd)
        Path(tmp_name).write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_name, self.state_path)

    def log(self, message: str):
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        print(line)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def connect(self, path: Path):
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def sort_key_for_max(self, candidate: dict):
        event_dt = candidate.get("event_dt") or datetime.fromtimestamp(0, tz=ZoneInfo("UTC"))
        priority = candidate.get("priority", 99)
        error_id = str(candidate.get("error_id", ""))
        return (event_dt, -priority, error_id)

    def sort_key_for_retry_desc(self, candidate: dict):
        retry_at = candidate.get("retry_at") or datetime.fromtimestamp(0, tz=ZoneInfo("UTC"))
        priority = candidate.get("priority", 99)
        event_dt = candidate.get("event_dt") or datetime.fromtimestamp(0, tz=ZoneInfo("UTC"))
        error_id = str(candidate.get("error_id", ""))
        return (retry_at, event_dt, -priority, error_id)

    def candidate_trust_rank(self, candidate: dict):
        source = candidate.get("source")
        signal_strength = candidate.get("signal_strength")
        if source == "logs" and signal_strength == "strong":
            return 3
        if source == "rollout":
            return 2
        if source == "logs":
            return 1
        return 0

    def choose_better_candidate(self, current: Optional[dict], challenger: Optional[dict]):
        if challenger is None:
            return current
        if current is None:
            return challenger
        if self.candidate_scope_rank(challenger) != self.candidate_scope_rank(current):
            return challenger if self.candidate_scope_rank(challenger) > self.candidate_scope_rank(current) else current
        if self.candidate_family_rank(challenger) != self.candidate_family_rank(current):
            return challenger if self.candidate_family_rank(challenger) > self.candidate_family_rank(current) else current
        if self.candidate_trust_rank(challenger) != self.candidate_trust_rank(current):
            return challenger if self.candidate_trust_rank(challenger) > self.candidate_trust_rank(current) else current
        if self.sort_key_for_max(challenger) != self.sort_key_for_max(current):
            return challenger if self.sort_key_for_max(challenger) > self.sort_key_for_max(current) else current
        return challenger if str(challenger.get("error_id", "")) > str(current.get("error_id", "")) else current

    def is_limit_fully_used(self, value) -> bool:
        try:
            return float(value) == 100.0
        except (TypeError, ValueError):
            return False

    def candidate_scope_rank(self, candidate: Optional[dict]):
        if not candidate:
            return 0
        if candidate.get("limit_scope") == "global_window":
            return 2
        if candidate.get("limit_scope") == "session_window":
            return 1
        return 0

    def candidate_family_rank(self, candidate: Optional[dict]):
        if not candidate:
            return 0
        family = candidate.get("candidate_family")
        if family == "secondary_credits_exhausted":
            return 4
        if family == "global_window_limit":
            return 3
        if family == "session_credits_exhausted":
            return 2
        if family == "session_window_limit":
            return 1
        return 0

    def choose_session_candidate(self, current: Optional[dict], challenger: Optional[dict]):
        if challenger is None:
            return current
        if current is None:
            return challenger
        if self.candidate_scope_rank(challenger) != self.candidate_scope_rank(current):
            return challenger if self.candidate_scope_rank(challenger) > self.candidate_scope_rank(current) else current
        challenger_time = challenger.get("scheduled_run_at") or challenger.get("retry_at")
        current_time = current.get("scheduled_run_at") or current.get("retry_at")
        if challenger_time != current_time:
            return challenger if challenger_time > current_time else current
        if self.candidate_family_rank(challenger) != self.candidate_family_rank(current):
            return challenger if self.candidate_family_rank(challenger) > self.candidate_family_rank(current) else current
        if self.candidate_trust_rank(challenger) != self.candidate_trust_rank(current):
            return challenger if self.candidate_trust_rank(challenger) > self.candidate_trust_rank(current) else current
        if self.sort_key_for_max(challenger) != self.sort_key_for_max(current):
            return challenger if self.sort_key_for_max(challenger) > self.sort_key_for_max(current) else current
        return challenger if str(challenger.get("error_id", "")) > str(current.get("error_id", "")) else current

    def choose_global_governing_candidate(self, current: Optional[dict], challenger: Optional[dict]):
        if challenger is None or not challenger.get("governs_all_sessions"):
            return current
        if current is None:
            return challenger
        challenger_time = challenger.get("scheduled_run_at") or challenger.get("retry_at")
        current_time = current.get("scheduled_run_at") or current.get("retry_at")
        if challenger_time != current_time:
            return challenger if challenger_time > current_time else current
        if self.candidate_family_rank(challenger) != self.candidate_family_rank(current):
            return challenger if self.candidate_family_rank(challenger) > self.candidate_family_rank(current) else current
        if self.candidate_trust_rank(challenger) != self.candidate_trust_rank(current):
            return challenger if self.candidate_trust_rank(challenger) > self.candidate_trust_rank(current) else current
        if self.sort_key_for_max(challenger) != self.sort_key_for_max(current):
            return challenger if self.sort_key_for_max(challenger) > self.sort_key_for_max(current) else current
        return challenger if str(challenger.get("error_id", "")) > str(current.get("error_id", "")) else current

    def is_candidate_expired(self, candidate: dict, now: datetime):
        scheduled_run_at = candidate.get("scheduled_run_at")
        if not scheduled_run_at:
            return True
        return scheduled_run_at <= now

    def normalize_candidate_metadata(self, candidate: dict):
        limit_kind = candidate.get("limit_kind") or ""
        retry_source = candidate.get("retry_source") or ""
        secondary_used = candidate.get("secondary_used_percent")
        global_scope = (
            "secondary" in limit_kind
            or "secondary" in retry_source
            or self.is_limit_fully_used(secondary_used)
        )
        if "credits_exhausted" in limit_kind or retry_source.startswith("credits."):
            family = "secondary_credits_exhausted" if global_scope else "session_credits_exhausted"
        elif global_scope:
            family = "global_window_limit"
        else:
            family = "session_window_limit"
        candidate["limit_scope"] = "global_window" if global_scope else "session_window"
        candidate["governs_all_sessions"] = global_scope
        candidate["candidate_family"] = family
        return candidate

    def log_state_db_fallback(self, context: str, exc: Exception):
        if context in self.state_db_warning_contexts:
            return
        self.state_db_warning_contexts.add(context)
        self.log(f"{context}: {exc}; fallback to rollout/cache")

    def default_thread_info(self, session_id: str):
        return {
            "id": session_id,
            "rollout_path": None,
            "title": None,
            "cwd": None,
            "model_provider": None,
            "created_at": None,
            "updated_at": None,
            "source": "minimal",
        }

    def merge_thread_info(self, *items):
        valid_items = [item for item in items if item]
        if not valid_items:
            return None
        session_id = None
        merged = {}
        source = "minimal"
        for item in valid_items:
            session_id = session_id or item.get("id")
            for key, value in item.items():
                if key == "source":
                    continue
                if value not in (None, ""):
                    merged[key] = value
            if item.get("source"):
                source = item["source"]
        merged["id"] = session_id
        merged["source"] = source
        defaults = self.default_thread_info(session_id)
        defaults.update(merged)
        return defaults

    def has_meaningful_thread_info(self, thread_info: Optional[dict]):
        if not thread_info:
            return False
        return any(
            thread_info.get(key)
            for key in ("rollout_path", "cwd", "title", "model_provider", "created_at", "updated_at")
        )

    def cache_thread_info(self, thread_info: Optional[dict], persist: bool = True):
        if not thread_info or not thread_info.get("id"):
            return
        session_id = thread_info["id"]
        existing = self.state["thread_cache"].get(session_id, {})
        merged = self.merge_thread_info(existing, thread_info)
        if not merged:
            return
        merged["last_verified_at"] = datetime.now().astimezone().isoformat()
        if existing == merged:
            return
        self.state["thread_cache"][session_id] = merged
        if persist:
            self.save_state()

    def get_cached_thread_info(self, session_id: str):
        info = self.state.get("thread_cache", {}).get(session_id)
        if not info:
            return None
        return self.merge_thread_info(self.default_thread_info(session_id), info)

    def build_job_error_key(self, job: dict):
        retry_at_text = job.get("origin_retry_at") or job.get("retry_at")
        error_id = job.get("origin_error_id") or job.get("error_log_id") or job.get("governing_error_id")
        session_id = job.get("session_id")
        if not retry_at_text or not error_id or not session_id:
            return None
        try:
            retry_at = datetime.fromisoformat(retry_at_text)
        except ValueError:
            return None
        return self.error_key(session_id, retry_at, error_id)

    def cleanup_state(self):
        pending_jobs = self.state.get("pending_jobs", [])
        if not pending_jobs:
            return

        cleaned_jobs = []
        changed = False
        for job in pending_jobs:
            if job.get("status") != "pending":
                cleaned_jobs.append(job)
                continue

            session_id = job.get("session_id")
            scheduled_run_at = job.get("scheduled_run_at")
            if not session_id or not scheduled_run_at:
                changed = True
                continue
            try:
                datetime.fromisoformat(scheduled_run_at)
            except ValueError:
                changed = True
                continue

            thread_info = self.thread_exists(session_id)
            if self.has_meaningful_thread_info(thread_info):
                normalized = dict(job)
                normalized["title"] = thread_info.get("title")
                normalized["rollout_path"] = thread_info.get("rollout_path")
                normalized["cwd"] = thread_info.get("cwd")
                if normalized != job:
                    changed = True
                    self.log(f"updated pending job metadata for session {session_id}")
                cleaned_jobs.append(normalized)
                continue

            cleaned_jobs.append(job)
        if changed:
            self.state["pending_jobs"] = cleaned_jobs
            self.save_state()
        now = datetime.now().astimezone()
        desired_jobs_by_session, _, _, allow_absent_prune = self.build_desired_pending_jobs(now=now)
        self.reconcile_pending_jobs(desired_jobs_by_session, now, allow_absent_prune=allow_absent_prune)

    def fetch_recent_logs(self):
        return self.fetch_logs_matching(limit=400)

    def fetch_logs_matching(self, limit: int = 400, session_id: Optional[str] = None, since_ts: Optional[int] = None):
        query = """
            select id, ts, level, thread_id, process_uuid, feedback_log_body
            from logs
            where feedback_log_body is not null
              and (
                feedback_log_body like '%run_turn: Turn error: You''ve hit your usage limit%'
                or feedback_log_body like '%startup_prewarm.resolve: startup websocket prewarm setup failed: You''ve hit your usage limit%'
                or feedback_log_body like '%You''ve hit your usage limit. Upgrade to Pro%'
                or feedback_log_body like '%"type":"usage_limit_reached"%'
                or feedback_log_body like '%"status_code":429%'
                or feedback_log_body like '%X-Codex-Primary-Used-Percent":"100"%'
              )
        """
        clauses = []
        params = []
        if session_id:
            clauses.append("thread_id = ?")
            params.append(session_id)
        if since_ts is not None:
            clauses.append("ts >= ?")
            params.append(since_ts)
        if clauses:
            query += "\n and " + "\n and ".join(clauses)
        query += "\n order by id desc\n limit ?"
        params.append(limit)
        with self.connect(self.logs_db) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [
            LogRow(
                id=row["id"],
                ts=row["ts"],
                level=row["level"],
                thread_id=row["thread_id"],
                process_uuid=row["process_uuid"],
                feedback_log_body=row["feedback_log_body"],
            )
            for row in rows
        ]

    def fetch_session_logs(self, session_id: str, limit: int = 200):
        return self.fetch_logs_matching(limit=limit, session_id=session_id)

    def fetch_recent_threads_from_state_db(self, limit: int = 20):
        query = """
            select id, rollout_path, title, cwd, model_provider, created_at, updated_at
            from threads
            order by updated_at desc
            limit ?
        """
        try:
            with self.connect(self.state_db) as conn:
                rows = conn.execute(query, (limit,)).fetchall()
        except sqlite3.OperationalError as exc:
            self.log_state_db_fallback("检查rollout时无法访问数据库", exc)
            return []
        return [self.thread_info_from_state_row(row) for row in rows]

    def fetch_thread_from_state_db(self, thread_id: str):
        query = """
            select id, rollout_path, title, cwd, model_provider, created_at, updated_at
            from threads
            where id = ?
        """
        try:
            with self.connect(self.state_db) as conn:
                row = conn.execute(query, (thread_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            self.log_state_db_fallback("检查日志时无法访问数据库 (thread_exists)", exc)
            return None
        if not row:
            return None
        return self.thread_info_from_state_row(row)

    def thread_info_from_state_row(self, row):
        thread_info = dict(row)
        thread_info["cwd"] = self.resolve_cwd_from_rollout(thread_info.get("rollout_path")) or thread_info.get("cwd")
        thread_info["source"] = "state_db"
        return thread_info

    def classify_row(self, row: LogRow) -> int:
        classification = self.classify_log_row(row)
        if not classification:
            return 0
        return classification["priority"]

    def classify_log_row(self, row: LogRow):
        text = row.feedback_log_body
        structured_usage_limit = (
            text.startswith("Received message {")
            and (
                '"type":"usage_limit_reached"' in text
                or ('"status_code":429' in text and "usage_limit" in text.lower())
                or ('X-Codex-Primary-Used-Percent":"100"' in text and RESETS_AT_RE.search(text))
            )
        )
        if structured_usage_limit:
            return {
                "priority": 3,
                "signal_strength": "weak",
                "reason": "structured websocket 429 usage-limit payload",
            }
        noise_markers = (
            "TurnDiffEvent:",
            "Received message ",
            "ToolCall:",
            "websocket request:",
        )
        if any(marker in text for marker in noise_markers):
            return None
        if "run_sampling_request" in text and "You've hit your usage limit" not in text and '"type":"usage_limit_reached"' not in text:
            return None
        if (
            "run_turn: Turn error: You've hit your usage limit" in text
            or "startup_prewarm.resolve: startup websocket prewarm setup failed: You've hit your usage limit"
            in text
        ):
            return {
                "priority": 1,
                "signal_strength": "strong",
                "reason": "explicit usage limit turn error",
            }
        if "You've hit your usage limit. Upgrade to Pro" in text and row.thread_id:
            return {
                "priority": 2,
                "signal_strength": "strong",
                "reason": "explicit upgrade-to-pro usage limit message",
            }
        if (
            '"type":"usage_limit_reached"' in text
            or ('"status_code":429' in text and "usage_limit" in text.lower())
            or ('X-Codex-Primary-Used-Percent":"100"' in text and RESETS_AT_RE.search(text))
        ):
            return {
                "priority": 3,
                "signal_strength": "weak",
                "reason": "rate-limit payload with resets_at or usage_limit marker",
            }
        return None

    def pick_latest_error(self, rows):
        candidates = []
        for row in rows:
            classification = self.classify_log_row(row)
            if classification:
                candidates.append((row, classification))
        if candidates:
            best_row, classification = max(
                candidates,
                key=lambda item: (
                    datetime.fromtimestamp(item[0].ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz),
                    -item[1]["priority"],
                    item[0].id,
                ),
            )
            return best_row, classification["priority"]
        return None, None

    def resolve_thread_id(self, rows, target_row: LogRow):
        if target_row.thread_id:
            return target_row.thread_id
        try:
            index = next(idx for idx, row in enumerate(rows) if row.id == target_row.id)
        except StopIteration:
            index = None
        if index is not None:
            for row in rows[index : min(len(rows), index + 20)]:
                if row.thread_id and abs(row.ts - target_row.ts) <= 5 and abs(row.id - target_row.id) <= 50:
                    return row.thread_id
            for row in rows[max(0, index - 20) : index]:
                if row.thread_id and abs(row.ts - target_row.ts) <= 5 and abs(row.id - target_row.id) <= 50:
                    return row.thread_id
        recovered_thread_id, _ = self.resolve_thread_id_via_process_uuid(target_row)
        return recovered_thread_id

    def fetch_rows_for_process_uuid(self, process_uuid: str, target_ts: int, window_seconds: int = 120, limit: int = 400):
        if not process_uuid:
            return []
        query = """
            select id, ts, level, thread_id, process_uuid, feedback_log_body
            from logs
            where process_uuid = ?
              and ts between ? and ?
            order by abs(ts - ?) asc, id desc
            limit ?
        """
        with self.connect(self.logs_db) as conn:
            rows = conn.execute(
                query,
                (
                    process_uuid,
                    max(0, target_ts - window_seconds),
                    target_ts + window_seconds,
                    target_ts,
                    limit,
                ),
            ).fetchall()
        return [
            LogRow(
                id=row["id"],
                ts=row["ts"],
                level=row["level"],
                thread_id=row["thread_id"],
                process_uuid=row["process_uuid"],
                feedback_log_body=row["feedback_log_body"],
            )
            for row in rows
        ]

    def resolve_thread_id_via_process_uuid(self, target_row: LogRow):
        process_uuid = target_row.process_uuid
        if not process_uuid:
            return None, None
        try:
            rows = self.fetch_rows_for_process_uuid(process_uuid, target_row.ts)
        except sqlite3.OperationalError:
            return None, None
        thread_rows = [row for row in rows if row.thread_id]
        if not thread_rows:
            return None, None
        unique_thread_ids = {row.thread_id for row in thread_rows if row.thread_id}
        if len(unique_thread_ids) == 1:
            return next(iter(unique_thread_ids)), "process_uuid_single_thread"
        prioritized_markers = (
            "session_loop{thread_id=",
            "turn{otel.name=\"session_task.turn\"",
            "thread.id=",
        )
        scored = []
        for row in thread_rows:
            score = 0
            text = row.feedback_log_body
            if any(marker in text for marker in prioritized_markers):
                score += 100
            score -= abs(row.ts - target_row.ts)
            score += min(50, max(0, 50 - abs(row.id - target_row.id)))
            scored.append((score, row))
        best_row = max(scored, key=lambda item: (item[0], item[1].id))[1]
        return best_row.thread_id, "process_uuid_nearest_context"

    def resolve_cwd_from_rollout(self, rollout_path: Optional[str]):
        info = self.parse_rollout_metadata(Path(rollout_path)) if rollout_path else None
        if not info:
            return None
        return info.get("cwd")

    def parse_iso_timestamp(self, value: str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(self.local_tz)

    def extract_json_object(self, text: str):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    def extract_retry_at_from_log_payload(self, text: str):
        payload = self.extract_json_object(text)
        if not isinstance(payload, dict):
            return None, None

        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            secondary = rate_limits.get("secondary")
            if isinstance(secondary, dict) and secondary.get("resets_at") and self.is_limit_fully_used(secondary.get("used_percent")):
                return int(secondary["resets_at"]), "rate_limits.secondary.resets_at"

        headers = payload.get("headers")
        if isinstance(headers, dict):
            secondary_used = headers.get("X-Codex-Secondary-Used-Percent")
            secondary_reset = headers.get("X-Codex-Secondary-Reset-At")
            if secondary_reset and self.is_limit_fully_used(secondary_used):
                return int(secondary_reset), "headers.secondary_reset_at"

        error = payload.get("error")
        if isinstance(error, dict):
            resets_at = error.get("resets_at")
            if resets_at:
                return int(resets_at), "error.resets_at"

        if isinstance(headers, dict):
            primary_reset = headers.get("X-Codex-Primary-Reset-At")
            if primary_reset:
                return int(primary_reset), "headers.primary_reset_at"

        if isinstance(rate_limits, dict):
            primary = rate_limits.get("primary")
            if isinstance(primary, dict) and primary.get("resets_at"):
                return int(primary["resets_at"]), "rate_limits.primary.resets_at"

        primary_reset_match = PRIMARY_RESET_AT_HEADER_RE.search(text)
        if primary_reset_match:
            return int(primary_reset_match.group(1)), "headers.primary_reset_at"
        return None, None

    def extract_usage_limit_payload(self, text: str):
        payload = self.extract_json_object(text)
        if not isinstance(payload, dict):
            return None
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
        primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
        secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}
        credits = rate_limits.get("credits") if isinstance(rate_limits.get("credits"), dict) else {}
        return {
            "payload_type": payload.get("type"),
            "status_code": payload.get("status_code"),
            "error_type": error.get("type"),
            "error_resets_at": error.get("resets_at"),
            "primary_reset_at": headers.get("X-Codex-Primary-Reset-At") or primary.get("resets_at"),
            "secondary_reset_at": headers.get("X-Codex-Secondary-Reset-At") or secondary.get("resets_at"),
            "primary_used_percent": primary.get("used_percent") or headers.get("X-Codex-Primary-Used-Percent"),
            "secondary_used_percent": secondary.get("used_percent") or headers.get("X-Codex-Secondary-Used-Percent"),
            "credits_has": credits.get("has_credits") or headers.get("X-Codex-Credits-Has-Credits"),
            "credits_balance": credits.get("balance") or headers.get("X-Codex-Credits-Balance"),
        }

    def parse_retry_time(self, rows, target_row: LogRow):
        event_dt = datetime.fromtimestamp(target_row.ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
        match = TIME_RE.search(target_row.feedback_log_body)
        if match:
            parsed_time = datetime.strptime(match.group(1), "%I:%M %p").time()
            retry_at = event_dt.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=0,
                microsecond=0,
            )
            if retry_at < event_dt - timedelta(minutes=1):
                retry_at += timedelta(days=1)
            return retry_at, "message"

        ordered_rows = [target_row] + [row for row in rows if row.id != target_row.id]
        for row in ordered_rows:
            reset_ts, source = self.extract_retry_at_from_log_payload(row.feedback_log_body)
            if reset_ts:
                retry_at = datetime.fromtimestamp(reset_ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
                return retry_at, source
        return None, None

    def group_rows_by_session(self, rows):
        grouped = {}
        for row in rows:
            if row.thread_id:
                grouped.setdefault(row.thread_id, []).append(row)
        return grouped

    def derive_session_id_from_rollout_path(self, rollout_path: Optional[str]):
        if not rollout_path:
            return None
        match = SESSION_ID_RE.search(str(rollout_path))
        if match:
            return match.group(1)
        return None

    def parse_rollout_metadata(self, path: Path, session_id: Optional[str] = None):
        if not path.exists():
            return None
        stat = path.stat()
        session_id = session_id or self.derive_session_id_from_rollout_path(str(path))
        info = {
            "id": session_id,
            "rollout_path": str(path),
            "title": None,
            "cwd": None,
            "model_provider": None,
            "created_at": int(stat.st_mtime),
            "updated_at": int(stat.st_mtime),
            "source": "rollout_scan",
        }
        try:
            with path.open(encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload", {})
                    if obj.get("timestamp"):
                        event_dt = self.parse_iso_timestamp(obj["timestamp"])
                        epoch = int(event_dt.timestamp())
                        if info["created_at"] is None or epoch < info["created_at"]:
                            info["created_at"] = epoch
                        if info["updated_at"] is None or epoch > info["updated_at"]:
                            info["updated_at"] = epoch
                    if obj.get("type") in ("turn_context", "session_meta") and isinstance(payload, dict):
                        if payload.get("cwd"):
                            info["cwd"] = payload["cwd"]
                        if payload.get("title"):
                            info["title"] = payload["title"]
                        if payload.get("model_provider"):
                            info["model_provider"] = payload["model_provider"]
                        if payload.get("first_user_message") and not info["title"]:
                            info["title"] = payload["first_user_message"][:120]
                    if info["cwd"] and info["title"] and info["model_provider"]:
                        break
        except Exception:
            return info
        return info

    def scan_rollout_index(self, force: bool = False):
        now = time.time()
        if not force and self.rollout_index_cache is not None and now - self.rollout_index_built_at < 30:
            return self.rollout_index_cache

        session_map = {}
        recent = []
        if self.sessions_root.exists():
            for path in self.sessions_root.rglob("*.jsonl"):
                session_id = self.derive_session_id_from_rollout_path(str(path))
                if not session_id:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                record = {
                    "session_id": session_id,
                    "rollout_path": str(path),
                    "mtime": stat.st_mtime,
                }
                existing = session_map.get(session_id)
                if (
                    existing is None
                    or record["mtime"] > existing["mtime"]
                    or (
                        record["mtime"] == existing["mtime"]
                        and record["rollout_path"] > existing["rollout_path"]
                    )
                ):
                    session_map[session_id] = record

        recent = sorted(session_map.values(), key=lambda item: item["mtime"], reverse=True)
        self.rollout_index_cache = {"by_session": session_map, "recent": recent}
        self.rollout_index_built_at = now
        return self.rollout_index_cache

    def get_rollout_record(self, session_id: str):
        index = self.scan_rollout_index()
        return index["by_session"].get(session_id)

    def get_thread_from_rollout(self, session_id: str):
        record = self.get_rollout_record(session_id)
        if not record:
            return None
        info = self.parse_rollout_metadata(Path(record["rollout_path"]), session_id=session_id)
        if info:
            self.cache_thread_info(info)
        return info

    def thread_exists(self, thread_id: str):
        minimal = self.default_thread_info(thread_id)
        cached = self.get_cached_thread_info(thread_id)
        rollout = self.get_thread_from_rollout(thread_id)
        state_db = self.fetch_thread_from_state_db(thread_id)
        merged = self.merge_thread_info(minimal, state_db, cached, rollout)
        if self.has_meaningful_thread_info(merged):
            self.cache_thread_info(merged)
            return merged
        self.log(f"using minimal thread metadata for session {thread_id}")
        return merged

    def fetch_recent_threads(self, limit: int = 20):
        threads = {}

        for record in self.scan_rollout_index()["recent"][:limit]:
            info = self.parse_rollout_metadata(Path(record["rollout_path"]), session_id=record["session_id"])
            if not info:
                continue
            self.cache_thread_info(info, persist=False)
            threads[record["session_id"]] = info

        for state_info in self.fetch_recent_threads_from_state_db(limit=limit):
            merged = self.merge_thread_info(threads.get(state_info["id"]), state_info)
            if state_info.get("cwd") and threads.get(state_info["id"], {}).get("cwd") is None:
                merged["cwd"] = state_info["cwd"]
            threads[state_info["id"]] = merged

        if self.state.get("thread_cache"):
            self.save_state()

        return sorted(
            threads.values(),
            key=lambda item: item.get("updated_at") or item.get("created_at") or 0,
            reverse=True,
        )[:limit]

    def inspect_rollout_for_usage_limit(self, thread_info: dict):
        candidates = self.collect_rollout_candidates_for_thread(thread_info)
        if not candidates:
            return None
        return max(candidates, key=self.sort_key_for_max)

    def build_rollout_candidate(
        self,
        thread_info: dict,
        event_dt: datetime,
        retry_at: datetime,
        line_no: int,
        text: str,
        retry_source: str,
        reason: str,
        limit_kind: str,
        primary_used: Optional[float],
        secondary_used: Optional[float],
        credits_has: Optional[bool],
        credits_balance: Optional[str],
    ):
        session_id = thread_info.get("id")
        if not session_id:
            return None
        candidate = {
            "source": "rollout",
            "priority": 2,
            "signal_strength": "rollout",
            "event_dt": event_dt,
            "error_id": f"rollout:{session_id}:{line_no}:{limit_kind}",
            "session_id": session_id,
            "retry_at": retry_at,
            "scheduled_run_at": retry_at + timedelta(minutes=10),
            "thread_info": thread_info,
            "message": text[:500],
            "message_preview": text[:180],
            "retry_source": retry_source,
            "reason": reason,
            "rollout_line": line_no,
            "limit_kind": limit_kind,
            "primary_used_percent": primary_used,
            "secondary_used_percent": secondary_used,
            "credits_has": credits_has,
            "credits_balance": credits_balance,
        }
        return self.normalize_candidate_metadata(candidate)

    def collect_rollout_candidates_for_thread(self, thread_info: dict):
        rollout_path = thread_info.get("rollout_path")
        if not rollout_path:
            return []
        path = Path(rollout_path)
        if not path.exists():
            return []

        candidates = []
        previous_state = None
        try:
            with path.open(encoding="utf-8") as handle:
                for line_no, raw_line in enumerate(handle, 1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    timestamp = obj.get("timestamp")
                    if not timestamp:
                        continue
                    event_dt = self.parse_iso_timestamp(timestamp)
                    text = line
                    payload = obj.get("payload", {})
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    rate_limits = payload.get("rate_limits", {}) if isinstance(payload, dict) else {}
                    limit_id = rate_limits.get("limit_id") if isinstance(rate_limits, dict) else None
                    primary = rate_limits.get("primary", {}) if isinstance(rate_limits, dict) else {}
                    secondary = rate_limits.get("secondary", {}) if isinstance(rate_limits, dict) else {}
                    credits = rate_limits.get("credits", {}) if isinstance(rate_limits, dict) else {}
                    primary_used = primary.get("used_percent") if isinstance(primary, dict) else None
                    primary_reset = primary.get("resets_at") if isinstance(primary, dict) else None
                    secondary_used = secondary.get("used_percent") if isinstance(secondary, dict) else None
                    secondary_reset = secondary.get("resets_at") if isinstance(secondary, dict) else None
                    credits_empty = False
                    credits_has = None
                    credits_balance = None
                    if isinstance(credits, dict):
                        credits_has = credits.get("has_credits")
                        credits_balance = credits.get("balance")
                        credits_empty = credits_has is False or credits_balance == "0"
                    current_state = {
                        "primary_active": self.is_limit_fully_used(primary_used) and primary_reset,
                        "primary_reset": primary_reset,
                        "secondary_active": self.is_limit_fully_used(secondary_used) and secondary_reset,
                        "secondary_reset": secondary_reset,
                        "credits_empty": credits_empty,
                        "limit_id": limit_id,
                    }
                    previous_state = previous_state or {
                        "primary_active": False,
                        "primary_reset": None,
                        "secondary_active": False,
                        "secondary_reset": None,
                        "credits_empty": False,
                        "limit_id": None,
                    }

                    if current_state["primary_active"] and not previous_state["primary_active"]:
                        retry_at = datetime.fromtimestamp(int(primary_reset), tz=ZoneInfo("UTC")).astimezone(
                            self.local_tz
                        )
                        candidate = self.build_rollout_candidate(
                            thread_info=thread_info,
                            event_dt=event_dt,
                            retry_at=retry_at,
                            line_no=line_no,
                            text=text,
                            retry_source="rate_limits.primary.resets_at",
                            reason="rollout primary usage limit reached",
                            limit_kind="rollout_primary_limit",
                            primary_used=primary_used,
                            secondary_used=secondary_used,
                            credits_has=credits_has,
                            credits_balance=credits_balance,
                        )
                        if candidate:
                            candidates.append(candidate)

                    if current_state["secondary_active"] and not previous_state["secondary_active"]:
                        retry_at = datetime.fromtimestamp(int(secondary_reset), tz=ZoneInfo("UTC")).astimezone(
                            self.local_tz
                        )
                        candidate = self.build_rollout_candidate(
                            thread_info=thread_info,
                            event_dt=event_dt,
                            retry_at=retry_at,
                            line_no=line_no,
                            text=text,
                            retry_source="rate_limits.secondary.resets_at",
                            reason="rollout secondary usage limit reached",
                            limit_kind="rollout_secondary_limit",
                            primary_used=primary_used,
                            secondary_used=secondary_used,
                            credits_has=credits_has,
                            credits_balance=credits_balance,
                        )
                        if candidate:
                            candidates.append(candidate)

                    if current_state["credits_empty"] and not previous_state["credits_empty"]:
                        reset_ts = None
                        retry_source = None
                        limit_kind = None
                        reason = None
                        if current_state["secondary_active"] and secondary_reset:
                            reset_ts = secondary_reset
                            retry_source = "credits.secondary.resets_at"
                            limit_kind = "rollout_secondary_credits_exhausted"
                            reason = "rollout credits exhausted while secondary limit is active"
                        elif current_state["primary_active"] and primary_reset:
                            reset_ts = primary_reset
                            retry_source = "credits.primary.resets_at"
                            limit_kind = "rollout_primary_credits_exhausted"
                            reason = "rollout credits exhausted while primary limit is active"
                        if reset_ts and retry_source and limit_kind and reason:
                            retry_at = datetime.fromtimestamp(int(reset_ts), tz=ZoneInfo("UTC")).astimezone(
                                self.local_tz
                            )
                            candidate = self.build_rollout_candidate(
                                thread_info=thread_info,
                                event_dt=event_dt,
                                retry_at=retry_at,
                                line_no=line_no,
                                text=text,
                                retry_source=retry_source,
                                reason=reason,
                                limit_kind=limit_kind,
                                primary_used=primary_used,
                                secondary_used=secondary_used,
                                credits_has=credits_has,
                                credits_balance=credits_balance,
                            )
                            if candidate:
                                candidates.append(candidate)

                    previous_state = current_state
        except Exception:
            return []
        return candidates

    def collect_rollout_candidates(self, days: int = 7, limit_threads: Optional[int] = None, session_id: Optional[str] = None):
        threshold = datetime.now().astimezone() - timedelta(days=days)
        if session_id:
            threads = [self.thread_exists(session_id)]
        else:
            thread_limit = limit_threads if limit_threads is not None else 200
            threshold_ts = threshold.timestamp()
            threads = []
            for record in self.scan_rollout_index()["recent"]:
                if record["mtime"] < threshold_ts:
                    break
                info = self.parse_rollout_metadata(Path(record["rollout_path"]), session_id=record["session_id"])
                if not info:
                    continue
                self.cache_thread_info(info, persist=False)
                threads.append(info)
                if len(threads) >= thread_limit:
                    break
            if self.state.get("thread_cache"):
                self.save_state()
        candidates = []
        for thread_info in threads:
            if not thread_info:
                continue
            thread_candidates = self.collect_rollout_candidates_for_thread(thread_info)
            for candidate in thread_candidates:
                if candidate["event_dt"] < threshold:
                    continue
                candidates.append(candidate)
        return candidates

    def inspect_latest_rollout_error(self):
        candidates = self.collect_rollout_candidates(days=14, limit_threads=50)
        if not candidates:
            self.log("no usage limit error found in recent rollouts")
            return None
        return max(candidates, key=self.sort_key_for_max)

    def error_key(self, session_id: str, retry_at: datetime, error_id):
        return f"{session_id}|{retry_at.isoformat()}|{error_id}"

    def collect_confirmed_candidates(self, days: int = 14, log_limit: int = 5000, rollout_limit_threads: int = 400):
        logs_available = True
        try:
            log_candidates = self.collect_log_candidates(days=days, limit=log_limit)
        except sqlite3.OperationalError as exc:
            self.log(f"检查日志时无法访问数据库: {exc}")
            log_candidates = []
            logs_available = False
        rollout_candidates = self.collect_rollout_candidates(days=days, limit_threads=rollout_limit_threads)
        candidates = self.dedupe_candidates(log_candidates + rollout_candidates)
        return candidates, logs_available

    def build_job_from_candidate(self, candidate: dict, governing_candidate: dict, now: datetime):
        thread_info = candidate.get("thread_info") or {}
        governing_thread_info = governing_candidate.get("thread_info") or {}
        return {
            "session_id": candidate["session_id"],
            "retry_at": governing_candidate["retry_at"].isoformat(),
            "scheduled_run_at": governing_candidate["scheduled_run_at"].isoformat(),
            "error_log_id": str(candidate["error_id"]),
            "status": "pending",
            "title": thread_info.get("title"),
            "rollout_path": thread_info.get("rollout_path"),
            "cwd": thread_info.get("cwd"),
            "limit_scope": governing_candidate.get("limit_scope"),
            "governs_all_sessions": governing_candidate.get("governs_all_sessions", False),
            "candidate_family": governing_candidate.get("candidate_family"),
            "origin_source": candidate.get("source"),
            "origin_error_id": str(candidate.get("error_id")),
            "origin_retry_at": candidate["retry_at"].isoformat(),
            "origin_scheduled_run_at": candidate["scheduled_run_at"].isoformat(),
            "origin_retry_source": candidate.get("retry_source"),
            "origin_reason": candidate.get("reason"),
            "origin_limit_scope": candidate.get("limit_scope"),
            "origin_candidate_family": candidate.get("candidate_family"),
            "governing_source": governing_candidate.get("source"),
            "governing_session_id": governing_candidate.get("session_id"),
            "governing_error_id": str(governing_candidate.get("error_id")),
            "governing_retry_at": governing_candidate["retry_at"].isoformat(),
            "governing_scheduled_run_at": governing_candidate["scheduled_run_at"].isoformat(),
            "governing_retry_source": governing_candidate.get("retry_source"),
            "governing_reason": governing_candidate.get("reason"),
            "governing_limit_scope": governing_candidate.get("limit_scope"),
            "governing_candidate_family": governing_candidate.get("candidate_family"),
            "governing_rollout_path": governing_thread_info.get("rollout_path"),
            "message_preview": candidate.get("message_preview"),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    def jobs_equivalent(self, current: dict, desired: dict):
        keys = (
            "session_id",
            "retry_at",
            "scheduled_run_at",
            "error_log_id",
            "limit_scope",
            "governs_all_sessions",
            "candidate_family",
            "origin_source",
            "origin_error_id",
            "origin_retry_at",
            "origin_scheduled_run_at",
            "origin_retry_source",
            "origin_reason",
            "origin_limit_scope",
            "origin_candidate_family",
            "governing_source",
            "governing_session_id",
            "governing_error_id",
            "governing_retry_at",
            "governing_scheduled_run_at",
            "governing_retry_source",
            "governing_reason",
            "governing_limit_scope",
            "governing_candidate_family",
            "governing_rollout_path",
        )
        return all(current.get(key) == desired.get(key) for key in keys)

    def build_desired_pending_jobs(self, now: Optional[datetime] = None, days: int = 14):
        now = now or datetime.now().astimezone()
        candidates, logs_available = self.collect_confirmed_candidates(days=days)
        active_candidates = []
        latest_per_session = {}
        global_governing_candidate = None
        for candidate in candidates:
            if self.is_candidate_expired(candidate, now):
                continue
            session_id = candidate.get("session_id")
            if not session_id:
                continue
            active_candidates.append(candidate)
            latest_per_session[session_id] = self.choose_session_candidate(latest_per_session.get(session_id), candidate)
            if candidate.get("governs_all_sessions"):
                global_governing_candidate = self.choose_global_governing_candidate(
                    global_governing_candidate,
                    candidate,
                )
        desired_jobs_by_session = {}
        for session_id, candidate in latest_per_session.items():
            governing_candidate = global_governing_candidate or candidate
            desired_jobs_by_session[session_id] = self.build_job_from_candidate(candidate, governing_candidate, now)
        return desired_jobs_by_session, global_governing_candidate, active_candidates, logs_available

    def reconcile_pending_jobs(self, desired_jobs_by_session: dict, now: datetime, allow_absent_prune: bool = True):
        pending_by_session = {}
        for job in self.state["pending_jobs"]:
            if job.get("status") == "pending" and job.get("session_id"):
                pending_by_session.setdefault(job["session_id"], []).append(job)

        changed = False
        for session_id, desired in desired_jobs_by_session.items():
            existing_jobs = pending_by_session.get(session_id, [])
            matched_job = None
            for job in existing_jobs:
                if self.jobs_equivalent(job, desired):
                    matched_job = job
                    break

            if matched_job:
                merged = dict(desired)
                merged["created_at"] = matched_job.get("created_at") or desired.get("created_at")
                merged["updated_at"] = now.isoformat()
                if merged != matched_job:
                    matched_job.clear()
                    matched_job.update(merged)
                    changed = True
                    self.log(f"updated pending job metadata for session {session_id}")
            else:
                for job in existing_jobs:
                    if job.get("status") != "pending":
                        continue
                    old_time = job.get("scheduled_run_at")
                    job["status"] = "replaced"
                    job["status_reason"] = "superseded_by_newer_candidate"
                    job["replaced_at"] = now.isoformat()
                    job["replacement_scheduled_run_at"] = desired["scheduled_run_at"]
                    changed = True
                    self.log(
                        f"replaced pending job for session {session_id}: "
                        f"{old_time} -> {desired['scheduled_run_at']}"
                    )
                self.state["pending_jobs"].append(desired)
                changed = True
                self.log(
                    f"scheduled session {session_id} retry_at={desired['retry_at']} "
                    f"scheduled_run_at={desired['scheduled_run_at']} "
                    f"origin_retry_at={desired['origin_retry_at']} "
                    f"governing_session={desired['governing_session_id']} "
                    f"governing_scope={desired['governing_limit_scope']}"
                )
                continue

            for job in existing_jobs:
                if job is matched_job or job.get("status") != "pending":
                    continue
                old_time = job.get("scheduled_run_at")
                job["status"] = "replaced"
                job["status_reason"] = "duplicate_pending_removed"
                job["replaced_at"] = now.isoformat()
                job["replacement_scheduled_run_at"] = desired["scheduled_run_at"]
                changed = True
                self.log(
                    f"replaced pending job for session {session_id}: "
                    f"{old_time} -> {desired['scheduled_run_at']}"
                )

        if allow_absent_prune:
            for session_id, existing_jobs in pending_by_session.items():
                if session_id in desired_jobs_by_session:
                    continue
                for job in existing_jobs:
                    if job.get("status") != "pending":
                        continue
                    job["status"] = "expired"
                    job["status_reason"] = "candidate_no_longer_active"
                    job["expired_at"] = now.isoformat()
                    changed = True
                    self.log(
                        f"expired pending job for session {session_id}: "
                        f"scheduled_run_at={job.get('scheduled_run_at')}"
                    )

        if changed:
            self.save_state()
        return changed

    def inspect_latest_log_error(self):
        try:
            rows = self.fetch_recent_logs()
        except sqlite3.OperationalError as exc:
            self.log(f"检查日志时无法访问数据库: {exc}")
            return None

        if not rows:
            return None

        latest_seen = max(row.id for row in rows)
        if latest_seen > self.state.get("last_seen_log_id", 0):
            self.state["last_seen_log_id"] = latest_seen
            self.save_state()

        candidates = self.collect_log_candidates_from_rows(rows)
        if not candidates:
            self.log("no usage limit error found in recent logs")
            return None
        result = max(candidates, key=self.sort_key_for_max)
        self.log(
            f"matched usage limit error priority={result['priority']} session_id={result['session_id']} "
            f"retry_source={result['retry_source']} retry_at={result['retry_at'].isoformat()} "
            f"scheduled={result['scheduled_run_at'].isoformat()}"
        )
        return result

    def inspect_log_error_for_session(self, session_id: str):
        try:
            rows = self.fetch_session_logs(session_id)
        except sqlite3.OperationalError as exc:
            self.log(f"debug session 读取日志失败: {exc}")
            return None
        candidates = self.collect_log_candidates_from_rows(rows, preferred_session_id=session_id)
        if not candidates:
            return None
        return max(candidates, key=self.sort_key_for_max)

    def collect_log_candidates(self, days: int = 7, session_id: Optional[str] = None, limit: int = 2000):
        since_dt = datetime.now(tz=self.local_tz) - timedelta(days=days)
        since_ts = int(since_dt.astimezone(ZoneInfo("UTC")).timestamp())
        rows = self.fetch_logs_matching(limit=limit, session_id=session_id, since_ts=since_ts)
        return self.collect_log_candidates_from_rows(rows, preferred_session_id=session_id)

    def collect_log_candidates_from_rows(self, rows, preferred_session_id: Optional[str] = None):
        grouped_rows = self.group_rows_by_session(rows)
        candidates = []
        for row in rows:
            classification = self.classify_log_row(row)
            if not classification:
                continue
            recovery_reason = None
            session_id = preferred_session_id or row.thread_id
            if not session_id:
                session_id, recovery_reason = self.resolve_thread_id_via_process_uuid(row)
            if not session_id:
                continue
            session_rows = grouped_rows.get(session_id, rows)
            retry_at, source = self.parse_retry_time(session_rows, row)
            if not retry_at:
                continue
            thread_info = self.thread_exists(session_id)
            event_dt = datetime.fromtimestamp(row.ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
            message = row.feedback_log_body[:500]
            payload_info = self.extract_usage_limit_payload(row.feedback_log_body) or {}
            candidate = {
                "source": "logs",
                "priority": classification["priority"],
                "signal_strength": classification["signal_strength"],
                "event_dt": event_dt,
                "error_id": row.id,
                "session_id": session_id,
                "retry_at": retry_at,
                "scheduled_run_at": retry_at + timedelta(minutes=10),
                "thread_info": thread_info,
                "message": message,
                "message_preview": message[:180],
                "retry_source": source,
                "reason": classification["reason"],
                "recovery_reason": recovery_reason,
                "process_uuid": row.process_uuid,
                "error_type": payload_info.get("error_type"),
                "status_code": payload_info.get("status_code"),
                "error_resets_at": payload_info.get("error_resets_at"),
                "primary_reset_at": payload_info.get("primary_reset_at"),
                "secondary_reset_at": payload_info.get("secondary_reset_at"),
                "primary_used_percent": payload_info.get("primary_used_percent"),
                "secondary_used_percent": payload_info.get("secondary_used_percent"),
                "credits_has": payload_info.get("credits_has"),
                "credits_balance": payload_info.get("credits_balance"),
            }
            candidates.append(self.normalize_candidate_metadata(candidate))
        return candidates

    def collect_recovered_structured_limit_events(self, days: int = 7, limit: int = 5000):
        since_dt = datetime.now(tz=self.local_tz) - timedelta(days=days)
        since_ts = int(since_dt.astimezone(ZoneInfo("UTC")).timestamp())
        rows = self.fetch_logs_matching(limit=limit, since_ts=since_ts)
        events = []
        seen = set()
        for row in rows:
            if row.thread_id:
                continue
            classification = self.classify_log_row(row)
            if not classification or classification["priority"] != 3:
                continue
            payload_info = self.extract_usage_limit_payload(row.feedback_log_body)
            if not payload_info:
                continue
            recovered_session_id, recovery_reason = self.resolve_thread_id_via_process_uuid(row)
            if not recovered_session_id:
                continue
            retry_at, retry_source = self.parse_retry_time(rows, row)
            if not retry_at:
                continue
            event_dt = datetime.fromtimestamp(row.ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
            thread_info = self.thread_exists(recovered_session_id)
            dedupe_key = (row.id, recovered_session_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidate = {
                "source": "logs_structured_429",
                "priority": classification["priority"],
                "signal_strength": classification["signal_strength"],
                "event_dt": event_dt,
                "error_id": row.id,
                "session_id": recovered_session_id,
                "retry_at": retry_at,
                "scheduled_run_at": retry_at + timedelta(minutes=10),
                "thread_info": thread_info,
                "retry_source": retry_source,
                "reason": classification["reason"],
                "recovery_reason": recovery_reason,
                "process_uuid": row.process_uuid,
                "error_type": payload_info.get("error_type"),
                "status_code": payload_info.get("status_code"),
                "error_resets_at": payload_info.get("error_resets_at"),
                "primary_reset_at": payload_info.get("primary_reset_at"),
                "secondary_reset_at": payload_info.get("secondary_reset_at"),
                "primary_used_percent": payload_info.get("primary_used_percent"),
                "secondary_used_percent": payload_info.get("secondary_used_percent"),
                "credits_has": payload_info.get("credits_has"),
                "credits_balance": payload_info.get("credits_balance"),
                "message_preview": row.feedback_log_body[:180],
            }
            events.append(self.normalize_candidate_metadata(candidate))
        events.sort(key=self.sort_key_for_retry_desc, reverse=True)
        return events

    def dedupe_candidates(self, candidates):
        deduped = {}
        for candidate in candidates:
            retry_at = candidate.get("retry_at")
            if not retry_at:
                continue
            key = (
                candidate.get("session_id"),
                retry_at.isoformat(),
                candidate.get("limit_scope"),
            )
            deduped[key] = self.choose_better_candidate(deduped.get(key), candidate)
        return list(deduped.values())

    def collect_suspected_limit_matches(self, days: int = 7, limit: int = 5000):
        since_dt = datetime.now(tz=self.local_tz) - timedelta(days=days)
        since_ts = int(since_dt.astimezone(ZoneInfo("UTC")).timestamp())
        rows = self.fetch_logs_matching(limit=limit, since_ts=since_ts)
        grouped = {}
        for row in rows:
            text = row.feedback_log_body
            if (
                "TurnDiffEvent:" in text
                or "Received message " in text
                or "ToolCall:" in text
                or "websocket request:" in text
                or "response.create" in text
                or "run_sampling_request" in text
                or "SSE event:" in text
            ):
                continue
            classification = self.classify_log_row(row)
            bucket = "confirmed" if classification else "suspected"
            if bucket == "suspected" and not (
                "You've hit your usage limit" in text
                or '"type":"usage_limit_reached"' in text
                or '"status_code":429' in text
                or 'X-Codex-Primary-Used-Percent":"100"' in text
            ):
                continue
            session_key = row.thread_id or "__none__"
            item = grouped.get(session_key)
            event_dt = datetime.fromtimestamp(row.ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
            if item is None:
                grouped[session_key] = {
                    "thread_id": row.thread_id,
                    "bucket": bucket,
                    "count": 1,
                    "first_event_dt": event_dt.isoformat(),
                    "last_event_dt": event_dt.isoformat(),
                    "sample_log_id": row.id,
                    "sample_preview": text[:220],
                    "reasons": [],
                }
            else:
                item["count"] += 1
                if event_dt.isoformat() < item["first_event_dt"]:
                    item["first_event_dt"] = event_dt.isoformat()
                if event_dt.isoformat() > item["last_event_dt"]:
                    item["last_event_dt"] = event_dt.isoformat()
                    item["sample_log_id"] = row.id
                    item["sample_preview"] = text[:220]
            if classification:
                reason = classification["reason"]
            elif "websocket request:" in text:
                reason = "contains usage-limit text inside websocket request payload"
            elif "run_sampling_request" in text:
                reason = "contains usage-limit phrase only in sampling/request trace"
            else:
                reason = "raw limit-like match excluded by classifier"
            if reason not in grouped[session_key]["reasons"]:
                grouped[session_key]["reasons"].append(reason)
        results = sorted(grouped.values(), key=lambda item: item["last_event_dt"], reverse=True)
        return results

    def inspect_latest_error(self):
        candidates, _ = self.collect_confirmed_candidates(days=14)
        if not candidates:
            self.log("no usage limit error found in recent logs or rollouts")
            return None
        best = max(candidates, key=self.sort_key_for_max)
        self.log(
            f"selected latest usage limit candidate source={best['source']} session_id={best['session_id']} "
            f"retry_source={best['retry_source']} retry_at={best['retry_at'].isoformat()} "
            f"scheduled={best['scheduled_run_at'].isoformat()} "
            f"scope={best.get('limit_scope')} governs_all={best.get('governs_all_sessions')} "
            f"cwd={best['thread_info'].get('cwd')}"
        )
        return best

    def trigger_due_jobs(self):
        now = datetime.now().astimezone()
        updated = False
        for job in self.state["pending_jobs"]:
            if job["status"] != "pending":
                continue
            scheduled_run_at = datetime.fromisoformat(job["scheduled_run_at"])
            if scheduled_run_at > now:
                continue
            command = [
                sys.executable,
                str((self.base_dir / "scripts" / "open_terminal_and_resume.py").resolve()),
                job["session_id"],
                job.get("cwd") or "",
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            triggered_entry = dict(job)
            triggered_entry["triggered_at"] = now.isoformat()
            if result.returncode == 0:
                job["status"] = "triggered"
                triggered_entry["status"] = "triggered"
                self.log(f"triggered terminal resume for session {job['session_id']}")
            else:
                job["status"] = "failed"
                triggered_entry["status"] = "failed"
                triggered_entry["stderr"] = result.stderr.strip()
                self.log(
                    f"failed to trigger terminal resume for session {job['session_id']}: "
                    f"{result.stderr.strip()}"
                )
            self.state["triggered_jobs"].append(triggered_entry)
            updated = True
        if updated:
            self.save_state()

    def run_once(self):
        now = datetime.now().astimezone()
        latest_result = self.inspect_latest_error()
        if latest_result:
            self.state["last_detected_error"] = latest_result["message"]
            self.state["last_detected_session_id"] = latest_result["session_id"]
            self.state["last_detected_retry_at"] = latest_result["retry_at"].isoformat()
            self.state["last_detected_scheduled_run_at"] = latest_result["scheduled_run_at"].isoformat()
            self.state["last_detected_cwd"] = latest_result["thread_info"].get("cwd")
            self.save_state()

        desired_jobs_by_session, global_governing_candidate, active_candidates, allow_absent_prune = self.build_desired_pending_jobs(
            now=now
        )
        if latest_result and not active_candidates:
            self.log("detected retry time is already in the past; mark as expired and skip scheduling")
        if active_candidates:
            self.log(
                f"reconciling pending jobs sessions={len(desired_jobs_by_session)} "
                f"global_override={'yes' if global_governing_candidate else 'no'}"
            )
        self.reconcile_pending_jobs(
            desired_jobs_by_session,
            now,
            allow_absent_prune=allow_absent_prune,
        )
        self.trigger_due_jobs()
        return 0

    def force_latest(self):
        result = self.inspect_latest_error()
        if not result:
            self.log("no latest detected session available for force execution")
            return 1
        session_id = result["session_id"]
        cwd = result["thread_info"].get("cwd") or ""
        self.state["last_detected_error"] = result["message"]
        self.state["last_detected_session_id"] = session_id
        self.state["last_detected_retry_at"] = result["retry_at"].isoformat()
        self.state["last_detected_scheduled_run_at"] = result["scheduled_run_at"].isoformat()
        self.state["last_detected_cwd"] = cwd
        self.save_state()
        command = [
            sys.executable,
            str((self.base_dir / "scripts" / "open_terminal_and_resume.py").resolve()),
            session_id,
            cwd,
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            self.log(f"force triggered terminal resume for session {session_id} in cwd {cwd}")
            return 0
        self.log(f"force trigger failed for session {session_id}: {result.stderr.strip()}")
        return 1

    def serialize_candidate(self, candidate: Optional[dict]):
        if not candidate:
            return None
        serialized = dict(candidate)
        for key in ("event_dt", "retry_at", "scheduled_run_at"):
            if serialized.get(key):
                serialized[key] = serialized[key].isoformat()
        if serialized.get("thread_info"):
            serialized["thread_info"] = dict(serialized["thread_info"])
        return serialized

    def format_candidate_brief(self, candidate: dict, index: int):
        thread_info = candidate.get("thread_info") or {}
        parts = [
            f"{index}. [{candidate.get('source')}] session={candidate.get('session_id')}",
            f"event={candidate.get('event_dt').isoformat() if candidate.get('event_dt') else '-'}",
            f"retry={candidate.get('retry_at').isoformat() if candidate.get('retry_at') else '-'}",
            f"retry_source={candidate.get('retry_source') or '-'}",
        ]
        line1 = " | ".join(parts)

        detail_parts = [
            f"scheduled={candidate.get('scheduled_run_at').isoformat() if candidate.get('scheduled_run_at') else '-'}",
            f"reason={candidate.get('reason') or '-'}",
            f"scope={candidate.get('limit_scope') or '-'}",
            f"governs_all={candidate.get('governs_all_sessions')}",
        ]
        if candidate.get("candidate_family"):
            detail_parts.append(f"family={candidate['candidate_family']}")
        if candidate.get("limit_kind"):
            detail_parts.append(f"kind={candidate['limit_kind']}")
        if candidate.get("rollout_line"):
            detail_parts.append(f"line={candidate['rollout_line']}")
        if candidate.get("process_uuid"):
            detail_parts.append(f"process_uuid={candidate['process_uuid']}")
        if thread_info.get("cwd"):
            detail_parts.append(f"cwd={thread_info['cwd']}")
        line2 = "   " + " | ".join(detail_parts)

        usage_parts = []
        if candidate.get("primary_used_percent") is not None:
            usage_parts.append(f"primary={candidate['primary_used_percent']}%")
        if candidate.get("secondary_used_percent") is not None:
            usage_parts.append(f"secondary={candidate['secondary_used_percent']}%")
        if candidate.get("credits_has") is not None:
            usage_parts.append(f"credits_has={candidate['credits_has']}")
        if candidate.get("credits_balance") not in (None, ""):
            usage_parts.append(f"credits_balance={candidate['credits_balance']}")
        if candidate.get("recovery_reason"):
            usage_parts.append(f"recovery={candidate['recovery_reason']}")
        line3 = f"   usage: {' | '.join(usage_parts)}" if usage_parts else None

        lines = [line1, line2]
        if line3:
            lines.append(line3)
        return "\n".join(lines)

    def format_suspected_match_brief(self, item: dict, index: int):
        reasons = ", ".join(item.get("reasons") or [])
        return (
            f"{index}. thread_id={item.get('thread_id') or 'null'} | bucket={item.get('bucket')} | "
            f"count={item.get('count')} | first={item.get('first_event_dt')} | last={item.get('last_event_dt')}\n"
            f"   sample_log_id={item.get('sample_log_id')} | reasons={reasons}"
        )

    def render_debug_section(self, title: str, items, formatter):
        lines = [f"{title} ({len(items)})"]
        if not items:
            lines.append("  无")
            return "\n".join(lines)
        for index, item in enumerate(items, 1):
            rendered = formatter(item, index).splitlines()
            lines.extend(rendered)
        return "\n".join(lines)

    def debug_limit_history(self, days: int = 7):
        log_candidates = self.collect_log_candidates(days=days, limit=5000)
        rollout_candidates = self.collect_rollout_candidates(days=days, limit_threads=400)
        recovered_structured_events = self.collect_recovered_structured_limit_events(days=days, limit=5000)
        candidates = self.dedupe_candidates(log_candidates + rollout_candidates)
        candidates.sort(key=self.sort_key_for_retry_desc, reverse=True)
        all_limit_events = sorted(log_candidates + rollout_candidates, key=self.sort_key_for_retry_desc, reverse=True)
        suspected_matches = self.collect_suspected_limit_matches(days=days, limit=5000)

        sections = [
            f"Usage Limit Debug 近 {days} 天",
            (
                "汇总: "
                f"all_limit_events={len(all_limit_events)} | "
                f"confirmed_candidates={len(candidates)} | "
                f"rollout_limit_events={len(rollout_candidates)} | "
                f"recovered_structured_429_events={len(recovered_structured_events)} | "
                f"suspected_matches={len(suspected_matches)}"
            ),
            self.render_debug_section(
                "Desired Pending Jobs",
                list(self.build_desired_pending_jobs(now=datetime.now().astimezone(), days=days)[0].values()),
                lambda job, index: (
                    f"{index}. session={job.get('session_id')} | scheduled={job.get('scheduled_run_at')} | "
                    f"origin_retry={job.get('origin_retry_at')} | scope={job.get('limit_scope')} | "
                    f"governing_session={job.get('governing_session_id')}"
                ),
            ),
            self.render_debug_section("Confirmed Candidates", candidates, self.format_candidate_brief),
            self.render_debug_section("Rollout Limit Events", rollout_candidates, self.format_candidate_brief),
            self.render_debug_section(
                "Recovered Structured 429 Events",
                recovered_structured_events,
                self.format_candidate_brief,
            ),
            self.render_debug_section("Suspected Matches", suspected_matches, self.format_suspected_match_brief),
        ]
        print("\n\n".join(sections))
        return 0

    def debug_session(self, session_id: str):
        thread_info = self.thread_exists(session_id)
        rollout_record = self.get_rollout_record(session_id)
        rollout_candidates = self.collect_rollout_candidates(days=30, session_id=session_id)
        log_candidates = self.collect_log_candidates(days=30, session_id=session_id, limit=1000)
        rollout_candidate = max(rollout_candidates, key=self.sort_key_for_max) if rollout_candidates else None
        log_candidate = max(log_candidates, key=self.sort_key_for_max) if log_candidates else None
        output = {
            "session_id": session_id,
            "codex_home": str(self.codex_home),
            "logs_db_exists": self.logs_db.exists(),
            "state_db_exists": self.state_db.exists(),
            "rollout_record": rollout_record,
            "thread_info": thread_info,
            "log_candidates_count": len(log_candidates),
            "rollout_candidates_count": len(rollout_candidates),
            "log_candidate": self.serialize_candidate(log_candidate),
            "rollout_candidate": self.serialize_candidate(rollout_candidate),
            "all_log_candidates": [self.serialize_candidate(item) for item in sorted(log_candidates, key=self.sort_key_for_retry_desc, reverse=True)],
            "all_rollout_candidates": [self.serialize_candidate(item) for item in sorted(rollout_candidates, key=self.sort_key_for_retry_desc, reverse=True)],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    def print_status(self):
        print(json.dumps(self.state, ensure_ascii=False, indent=2))
        return 0

    def compute_sleep_seconds(self, now: Optional[datetime] = None):
        now = now or datetime.now().astimezone()
        sleep_seconds = float(self.poll_interval)
        nearest_pending_seconds = None
        for job in self.state["pending_jobs"]:
            if job.get("status") != "pending":
                continue
            scheduled_run_at_text = job.get("scheduled_run_at")
            if not scheduled_run_at_text:
                continue
            try:
                scheduled_run_at = datetime.fromisoformat(scheduled_run_at_text)
            except ValueError:
                continue
            seconds_until_due = (scheduled_run_at - now).total_seconds()
            if seconds_until_due <= 0:
                return 1.0
            if nearest_pending_seconds is None or seconds_until_due < nearest_pending_seconds:
                nearest_pending_seconds = seconds_until_due
        if nearest_pending_seconds is not None:
            sleep_seconds = min(sleep_seconds, nearest_pending_seconds)
        return max(1.0, sleep_seconds)

    def run_forever(self):
        self.log("starting usage limit watcher")
        while True:
            try:
                self.trigger_due_jobs()
                self.run_once()
            except Exception:
                error_message = f"watcher error:\n{traceback.format_exc()}"
                self.log(error_message)
                try:
                    self.trigger_due_jobs()
                except Exception as trigger_exc:
                    self.log(f"trigger check failed after watcher error: {trigger_exc}")
            time.sleep(self.compute_sleep_seconds())

    def run_sample_test(self):
        fake_row = LogRow(
            id=1,
            ts=int(datetime(2026, 6, 25, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()),
            level="INFO",
            thread_id="019efc0b-a83a-7890-8c03-5867370ccde6",
            process_uuid="pid:test:sample",
            feedback_log_body=(
                "Turn error: You've hit your usage limit. Upgrade to Pro "
                "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
                "to purchase more credits or try again at 3:13 PM."
            ),
        )
        retry_at, source = self.parse_retry_time([fake_row], fake_row)
        scheduled_run_at = retry_at + timedelta(minutes=10)
        result = {
            "session_id": fake_row.thread_id,
            "retry_source": source,
            "retry_at": retry_at.isoformat(),
            "scheduled_run_at": scheduled_run_at.isoformat(),
            "resume_command": (
                f'codex resume -m gpt-5.4 -c model_reasoning_effort=\'medium\' '
                f'--yolo {fake_row.thread_id} "continue"'
            ),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


def main():
    parser = argparse.ArgumentParser(description="Watch Codex usage-limit errors and schedule resumes.")
    parser.add_argument("--once", action="store_true", help="Scan once and update local queue")
    parser.add_argument("--status", action="store_true", help="Print watcher status")
    parser.add_argument("--test-sample", action="store_true", help="Run sample parser test")
    parser.add_argument("--force-latest", action="store_true", help="Force trigger the latest detected session")
    parser.add_argument("--debug-session", help="Print merged metadata and usage-limit candidates for a session")
    parser.add_argument("--debug-limit-history", action="store_true", help="Print recent limit candidates sorted by retry_at desc")
    parser.add_argument("--days", type=int, default=7, help="Day window for debug history/session candidate collection")
    args = parser.parse_args()

    cleanup_on_init = not (
        args.status
        or args.test_sample
        or args.debug_session
        or args.debug_limit_history
    )
    watcher = UsageLimitWatcher(Path(__file__).resolve().parent.parent, cleanup_on_init=cleanup_on_init)

    if args.status:
        return watcher.print_status()
    if args.test_sample:
        return watcher.run_sample_test()
    if args.force_latest:
        return watcher.force_latest()
    if args.debug_session:
        return watcher.debug_session(args.debug_session)
    if args.debug_limit_history:
        return watcher.debug_limit_history(days=args.days)
    if args.once:
        return watcher.run_once()
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

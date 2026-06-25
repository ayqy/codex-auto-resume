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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TIME_RE = re.compile(r"try again at ([0-9]{1,2}:[0-9]{2} [AP]M)")
RESETS_AT_RE = re.compile(r'"resets_at":\s*(\d+)')
SESSION_ID_RE = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$")


@dataclass
class LogRow:
    id: int
    ts: int
    level: str
    thread_id: Optional[str]
    feedback_log_body: str


class UsageLimitWatcher:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.logs_db = self.codex_home / "logs_2.sqlite"
        self.state_db = self.codex_home / "state_5.sqlite"
        self.tmp_dir = self.base_dir / "tmp"
        self.logs_dir = self.tmp_dir / "logs"
        self.state_path = self.tmp_dir / "state.json"
        self.log_path = self.logs_dir / "watcher.log"
        self.local_tz = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
        self.poll_interval = 300
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.state = self.load_state()
        self.cleanup_state()

    def load_state(self):
        if not self.state_path.exists():
            state = {
                "last_seen_log_id": 0,
                "processed_error_keys": [],
                "pending_jobs": [],
                "triggered_jobs": [],
                "last_detected_error": None,
                "last_detected_session_id": None,
                "last_detected_retry_at": None,
                "last_detected_scheduled_run_at": None,
                "last_detected_cwd": None,
            }
            self.save_state(state)
            return state
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state=None):
        if state is not None:
            self.state = state
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="state.", suffix=".json", dir=str(self.tmp_dir))
        os.close(tmp_fd)
        Path(tmp_name).write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_name, self.state_path)

    def parse_state_dt(self, value: Optional[str]):
        if not value:
            return None
        return datetime.fromisoformat(value)

    def build_job_error_key(self, job: dict):
        retry_at_text = job.get("retry_at")
        error_id = job.get("error_log_id")
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
        removed_keys = set()
        changed = False
        for job in pending_jobs:
            if job.get("status") != "pending":
                cleaned_jobs.append(job)
                continue

            session_id = job.get("session_id")
            if not session_id:
                key = self.build_job_error_key(job)
                if key:
                    removed_keys.add(key)
                changed = True
                continue

            thread_info = self.thread_exists(session_id)
            candidate = self.inspect_rollout_for_usage_limit(thread_info) if thread_info else None
            if not thread_info or not candidate:
                key = self.build_job_error_key(job)
                if key:
                    removed_keys.add(key)
                changed = True
                self.log(f"removed stale pending job for session {session_id}")
                continue

            normalized = dict(job)
            normalized["retry_at"] = candidate["retry_at"].isoformat()
            normalized["scheduled_run_at"] = candidate["scheduled_run_at"].isoformat()
            normalized["error_log_id"] = str(candidate["error_id"])
            normalized["title"] = candidate["thread_info"].get("title")
            normalized["rollout_path"] = candidate["thread_info"].get("rollout_path")
            normalized["cwd"] = candidate["thread_info"].get("cwd")
            if normalized != job:
                old_key = self.build_job_error_key(job)
                if old_key:
                    removed_keys.add(old_key)
                changed = True
                self.log(f"normalized pending job for session {session_id}")
            cleaned_jobs.append(normalized)

        if removed_keys:
            self.state["processed_error_keys"] = [
                key for key in self.state.get("processed_error_keys", []) if key not in removed_keys
            ]
        if changed:
            self.state["pending_jobs"] = cleaned_jobs
            self.save_state()

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

    def fetch_recent_logs(self):
        query = """
            select id, ts, level, thread_id, feedback_log_body
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
            order by id desc
            limit 200
        """
        with self.connect(self.logs_db) as conn:
            rows = conn.execute(query).fetchall()
        return [
            LogRow(
                id=row["id"],
                ts=row["ts"],
                level=row["level"],
                thread_id=row["thread_id"],
                feedback_log_body=row["feedback_log_body"],
            )
            for row in rows
        ]

    def fetch_recent_threads(self, limit: int = 20):
        query = """
            select id, rollout_path, title, cwd, model_provider, created_at, updated_at
            from threads
            order by updated_at desc
            limit ?
        """
        with self.connect(self.state_db) as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        threads = []
        for row in rows:
            thread_info = dict(row)
            thread_info["cwd"] = self.resolve_cwd_from_rollout(thread_info.get("rollout_path")) or thread_info.get("cwd")
            threads.append(thread_info)
        return threads

    def classify_row(self, row: LogRow) -> int:
        text = row.feedback_log_body
        noise_markers = (
            "TurnDiffEvent:",
            "Received message ",
            "ToolCall:",
            "websocket request:",
        )
        if any(marker in text for marker in noise_markers):
            return 0
        if (
            "run_turn: Turn error: You've hit your usage limit" in text
            or "startup_prewarm.resolve: startup websocket prewarm setup failed: You've hit your usage limit"
            in text
        ):
            return 1
        if "You've hit your usage limit. Upgrade to Pro" in text and row.thread_id:
            return 2
        if (
            '"type":"usage_limit_reached"' in text
            or '"status_code":429' in text
            or 'X-Codex-Primary-Used-Percent":"100"' in text
        ):
            return 3
        return 0

    def pick_latest_error(self, rows):
        buckets = {1: [], 2: [], 3: []}
        for row in rows:
            priority = self.classify_row(row)
            if priority:
                buckets[priority].append(row)
        for priority in (1, 2, 3):
            if buckets[priority]:
                return max(buckets[priority], key=lambda item: item.id), priority
        return None, None

    def resolve_thread_id(self, rows, target_row: LogRow):
        if target_row.thread_id:
            return target_row.thread_id
        try:
            index = next(idx for idx, row in enumerate(rows) if row.id == target_row.id)
        except StopIteration:
            return None
        for row in rows[index : min(len(rows), index + 20)]:
            if row.thread_id:
                return row.thread_id
        for row in rows[max(0, index - 20) : index]:
            if row.thread_id:
                return row.thread_id
        return None

    def thread_exists(self, thread_id: str):
        query = """
            select id, rollout_path, title, cwd, model_provider, created_at, updated_at
            from threads
            where id = ?
        """
        with self.connect(self.state_db) as conn:
            row = conn.execute(query, (thread_id,)).fetchone()
        if not row:
            return None
        thread_info = dict(row)
        resolved_cwd = self.resolve_cwd_from_rollout(thread_info.get("rollout_path")) or thread_info.get("cwd")
        thread_info["cwd"] = resolved_cwd
        return thread_info

    def resolve_cwd_from_rollout(self, rollout_path: Optional[str]):
        if not rollout_path:
            return None
        path = Path(rollout_path)
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    obj = json.loads(line)
                    if obj.get("type") == "turn_context":
                        cwd = obj.get("payload", {}).get("cwd")
                        if cwd:
                            return cwd
                    if obj.get("type") == "session_meta":
                        cwd = obj.get("payload", {}).get("cwd")
                        if cwd:
                            return cwd
        except Exception:
            return None
        return None

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

        for row in rows:
            reset_match = RESETS_AT_RE.search(row.feedback_log_body)
            if reset_match:
                retry_at = datetime.fromtimestamp(int(reset_match.group(1)), tz=ZoneInfo("UTC")).astimezone(
                    self.local_tz
                )
                return retry_at, "resets_at"
        return None, None

    def parse_iso_timestamp(self, value: str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(self.local_tz)

    def parse_retry_time_from_text(self, text: str, event_dt: datetime):
        match = TIME_RE.search(text)
        if not match:
            return None, None
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

    def derive_session_id_from_rollout_path(self, rollout_path: Optional[str]):
        if not rollout_path:
            return None
        match = SESSION_ID_RE.search(rollout_path)
        if match:
            return match.group(1)
        return None

    def inspect_rollout_for_usage_limit(self, thread_info: dict):
        rollout_path = thread_info.get("rollout_path")
        if not rollout_path:
            return None
        path = Path(rollout_path)
        if not path.exists():
            return None

        latest_candidate = None
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

                    timestamp = obj.get("timestamp")
                    if not timestamp:
                        continue
                    event_dt = self.parse_iso_timestamp(timestamp)
                    text = line
                    retry_at = None
                    retry_source = None
                    payload = obj.get("payload", {})
                    rate_limits = payload.get("rate_limits", {}) if isinstance(payload, dict) else {}
                    primary = rate_limits.get("primary", {}) if isinstance(rate_limits, dict) else {}
                    credits = rate_limits.get("credits", {}) if isinstance(rate_limits, dict) else {}
                    used_percent = primary.get("used_percent") if isinstance(primary, dict) else None
                    resets_at = primary.get("resets_at") if isinstance(primary, dict) else None
                    credits_empty = False
                    if isinstance(credits, dict):
                        credits_empty = credits.get("has_credits") is False or credits.get("balance") == "0"
                    saturation_hit = used_percent == 100.0 and credits_empty and resets_at

                    if not saturation_hit:
                        continue

                    priority = 2
                    retry_at = datetime.fromtimestamp(int(resets_at), tz=ZoneInfo("UTC")).astimezone(self.local_tz)
                    retry_source = "resets_at"

                    if not retry_at:
                        continue

                    session_id = thread_info.get("id") or self.derive_session_id_from_rollout_path(rollout_path)
                    if not session_id:
                        continue

                    candidate = {
                        "source": "rollout",
                        "priority": priority,
                        "event_dt": event_dt,
                        "error_id": f"rollout:{session_id}:{int(event_dt.timestamp())}:{priority}",
                        "session_id": session_id,
                        "retry_at": retry_at,
                        "scheduled_run_at": retry_at + timedelta(minutes=10),
                        "thread_info": thread_info,
                        "message": text[:500],
                        "retry_source": retry_source,
                    }
                    if (
                        latest_candidate is None
                        or candidate["event_dt"] > latest_candidate["event_dt"]
                        or (
                            candidate["event_dt"] == latest_candidate["event_dt"]
                            and candidate["priority"] < latest_candidate["priority"]
                        )
                    ):
                        latest_candidate = candidate
        except Exception:
            return None
        return latest_candidate

    def inspect_latest_rollout_error(self):
        candidates = []
        for thread_info in self.fetch_recent_threads(limit=20):
            candidate = self.inspect_rollout_for_usage_limit(thread_info)
            if candidate:
                candidates.append(candidate)
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item["event_dt"], -item["priority"]))

    def error_key(self, session_id: str, retry_at: datetime, error_id):
        return f"{session_id}|{retry_at.isoformat()}|{error_id}"

    def find_pending_for_session(self, session_id: str):
        for job in self.state["pending_jobs"]:
            if job["session_id"] == session_id and job["status"] == "pending":
                return job
        return None

    def replace_existing_job(self, session_id: str, scheduled_run_at: datetime):
        for job in self.state["pending_jobs"]:
            if job["session_id"] == session_id and job["status"] == "pending":
                old_time = datetime.fromisoformat(job["scheduled_run_at"])
                if old_time != scheduled_run_at:
                    job["status"] = "replaced"
                    self.log(
                        f"replaced pending job for session {session_id}: "
                        f"{old_time.isoformat()} -> {scheduled_run_at.isoformat()}"
                    )

    def add_job(self, session_id: str, retry_at: datetime, scheduled_run_at: datetime, error_id, metadata: dict):
        key = self.error_key(session_id, retry_at, error_id)
        if key in self.state["processed_error_keys"]:
            self.log(f"skip duplicate error key {key}")
            return False

        existing = self.find_pending_for_session(session_id)
        if existing and existing["scheduled_run_at"] == scheduled_run_at.isoformat():
            self.log(f"skip existing pending job for session {session_id} at {scheduled_run_at.isoformat()}")
            self.state["processed_error_keys"].append(key)
            self.save_state()
            return False

        self.replace_existing_job(session_id, scheduled_run_at)
        job = {
            "session_id": session_id,
            "retry_at": retry_at.isoformat(),
            "scheduled_run_at": scheduled_run_at.isoformat(),
            "error_log_id": str(error_id),
            "status": "pending",
            "title": metadata.get("title"),
            "rollout_path": metadata.get("rollout_path"),
            "cwd": metadata.get("cwd"),
        }
        self.state["pending_jobs"].append(job)
        self.state["processed_error_keys"].append(key)
        self.state["last_detected_error"] = metadata["message"]
        self.state["last_detected_session_id"] = session_id
        self.state["last_detected_retry_at"] = retry_at.isoformat()
        self.state["last_detected_scheduled_run_at"] = scheduled_run_at.isoformat()
        self.state["last_detected_cwd"] = metadata.get("cwd")
        self.save_state()
        self.log(
            f"scheduled session {session_id} retry_at={retry_at.isoformat()} "
            f"scheduled_run_at={scheduled_run_at.isoformat()}"
        )
        return True

    def inspect_latest_log_error(self):
        rows = self.fetch_recent_logs()
        if not rows:
            return None

        latest_seen = max(row.id for row in rows)
        if latest_seen > self.state.get("last_seen_log_id", 0):
            self.state["last_seen_log_id"] = latest_seen
            self.save_state()

        target_row, priority = self.pick_latest_error(rows)
        if not target_row:
            self.log("no usage limit error found in recent logs")
            return None

        session_id = self.resolve_thread_id(rows, target_row)
        if not session_id:
            self.log(f"failed to resolve session id for log {target_row.id}")
            return None

        thread_info = self.thread_exists(session_id)
        if not thread_info:
            self.log(f"resolved session id {session_id} but thread record not found")
            return None

        retry_at, source = self.parse_retry_time(rows, target_row)
        if not retry_at:
            self.log(f"failed to parse retry time for session {session_id}")
            return None

        event_dt = datetime.fromtimestamp(target_row.ts, tz=ZoneInfo("UTC")).astimezone(self.local_tz)
        scheduled_run_at = retry_at + timedelta(minutes=10)
        message = target_row.feedback_log_body[:500]
        self.log(
            f"matched usage limit error priority={priority} session_id={session_id} "
            f"retry_source={source} retry_at={retry_at.isoformat()} scheduled={scheduled_run_at.isoformat()}"
        )

        return {
            "source": "logs",
            "priority": priority,
            "event_dt": event_dt,
            "error_id": target_row.id,
            "session_id": session_id,
            "retry_at": retry_at,
            "scheduled_run_at": scheduled_run_at,
            "thread_info": thread_info,
            "message": message,
            "retry_source": source,
        }

    def inspect_latest_error(self):
        log_result = self.inspect_latest_log_error()
        rollout_result = self.inspect_latest_rollout_error()
        candidates = [item for item in (log_result, rollout_result) if item]
        if not candidates:
            self.log("no usage limit error found in recent logs or rollouts")
            return None
        best = max(candidates, key=lambda item: (item["event_dt"], -item["priority"]))
        self.log(
            f"selected latest usage limit candidate source={best['source']} session_id={best['session_id']} "
            f"retry_source={best['retry_source']} retry_at={best['retry_at'].isoformat()} "
            f"scheduled={best['scheduled_run_at'].isoformat()} cwd={best['thread_info'].get('cwd')}"
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
                job["cwd"],
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
        result = self.inspect_latest_error()
        if not result:
            return 0

        self.state["last_detected_error"] = result["message"]
        self.state["last_detected_session_id"] = result["session_id"]
        self.state["last_detected_retry_at"] = result["retry_at"].isoformat()
        self.state["last_detected_scheduled_run_at"] = result["scheduled_run_at"].isoformat()
        self.state["last_detected_cwd"] = result["thread_info"].get("cwd")
        self.save_state()

        now = datetime.now().astimezone()
        if result["scheduled_run_at"] <= now:
            self.log("detected retry time is already in the past; mark as expired and skip scheduling")
            return 0

        self.add_job(
            result["session_id"],
            result["retry_at"],
            result["scheduled_run_at"],
            result["error_id"],
            {
                "message": result["message"],
                "title": result["thread_info"].get("title"),
                "rollout_path": result["thread_info"].get("rollout_path"),
                "cwd": result["thread_info"].get("cwd"),
            },
        )
        self.trigger_due_jobs()
        return 0

    def force_latest(self):
        result = self.inspect_latest_error()
        if not result:
            self.log("no latest detected session available for force execution")
            return 1
        session_id = result["session_id"]
        cwd = result["thread_info"].get("cwd")
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

    def print_status(self):
        print(json.dumps(self.state, ensure_ascii=False, indent=2))
        return 0

    def run_forever(self):
        self.log("starting usage limit watcher")
        while True:
            try:
                self.trigger_due_jobs()
                self.run_once()
            except Exception as exc:
                self.log(f"watcher error: {exc}")
                try:
                    self.trigger_due_jobs()
                except Exception as trigger_exc:
                    self.log(f"trigger check failed after watcher error: {trigger_exc}")
            time.sleep(self.poll_interval)

    def run_sample_test(self):
        fake_row = LogRow(
            id=1,
            ts=int(datetime(2026, 6, 25, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()),
            level="INFO",
            thread_id="019efc0b-a83a-7890-8c03-5867370ccde6",
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
    args = parser.parse_args()

    watcher = UsageLimitWatcher(Path(__file__).resolve().parent.parent)

    if args.status:
        return watcher.print_status()
    if args.test_sample:
        return watcher.run_sample_test()
    if args.force_latest:
        return watcher.force_latest()
    if args.once:
        return watcher.run_once()
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

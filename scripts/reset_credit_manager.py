#!/usr/bin/env python3
"""One-shot, fail-closed scheduler for a Codex usage-limit reset credit."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from codex_reset_transport import (
    BEIJING_TZ,
    CodexAppServerClient,
    CrossValidationResult,
    ResetCredit,
    ResetSnapshot,
    ResetTransportError,
    default_cross_validate,
    format_beijing_time,
    public_credit_rows,
    select_earliest_available_credit,
    validate_snapshot_pair,
)


BASE_DIR = Path(__file__).resolve().parent.parent
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "codex-auto-resume" / "reset-credit"
RUNTIME_DIR = Path(os.environ.get("CODEX_RESET_RUNTIME_DIR", APP_SUPPORT_DIR / "state"))
PRIVATE_OPERATION_PATH = RUNTIME_DIR / "private-operation.json"
STATUS_PATH = RUNTIME_DIR / "status.json"
EVENT_LOG_PATH = RUNTIME_DIR / "events.log"
LOCK_PATH = RUNTIME_DIR / "manager.lock"
TUI_FALLBACK_PATH = BASE_DIR / "scripts" / "official_usage_fallback.py"
SOURCE_RESCUE_PATH = Path(
    os.environ.get(
        "CODEX_SOURCE_RESCUE_PATH",
        BASE_DIR / "scripts" / "reset_credit_rescue.py",
    )
)
DEFAULT_BEFORE_SECONDS = 3600
DEFAULT_FALLBACK_AFTER_SECONDS = 600
DEFAULT_SOURCE_RESCUE_AFTER_SECONDS = 720
DEFAULT_TARGET_AFTER_SECONDS = 3000
PRECHECK_OFFSETS = (900, 120)
SOURCE_RETRY_OFFSETS = (1200, 1800, 2400, 2940)
TERMINAL_STATUSES = {"completed", "expired", "disabled"}


def epoch_now() -> int:
    return int(time.time())


def iso_beijing(epoch: Optional[int]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=BEIJING_TZ).isoformat()


def atomic_json_write(path: Path, payload: dict, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResetTransportError("not-armed", "no reset-credit operation is armed") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ResetTransportError("invalid-state", "reset-credit state is unreadable") from exc
    if not isinstance(value, dict):
        raise ResetTransportError("invalid-state", "reset-credit state is invalid")
    return value


def public_operation(operation: dict) -> dict:
    return {
        "version": operation.get("version"),
        "status": operation.get("status"),
        "authorized": bool(operation.get("authorize_consume")),
        "expires_at": iso_beijing(operation.get("expires_at")),
        "trigger_at": iso_beijing(operation.get("trigger_at")),
        "fallback_at": iso_beijing(operation.get("fallback_at")),
        "source_rescue_at": iso_beijing(operation.get("source_rescue_at")),
        "target_at": iso_beijing(operation.get("target_at")),
        "last_event": operation.get("last_event"),
        "last_outcome": operation.get("last_outcome"),
        "attempt_count": len(operation.get("attempts", [])),
        "updated_at": iso_beijing(operation.get("updated_at")),
        "pid": operation.get("pid"),
    }


def save_operation(operation: dict):
    operation["updated_at"] = epoch_now()
    atomic_json_write(PRIVATE_OPERATION_PATH, operation, mode=0o600)
    atomic_json_write(STATUS_PATH, public_operation(operation), mode=0o600)


def log_event(event: str, **fields):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    allowed = {
        "transport",
        "outcome",
        "category",
        "status",
        "scheduled_at",
        "attempt",
        "reason",
    }
    safe_fields = {key: value for key, value in fields.items() if key in allowed and value is not None}
    line = json.dumps(
        {"at": iso_beijing(epoch_now()), "event": event, **safe_fields},
        ensure_ascii=False,
        sort_keys=True,
    )
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(line, flush=True)


@contextmanager
def manager_lock(blocking: bool = False):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise ResetTransportError("already-running", "another reset-credit manager is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def print_credit_table(snapshot: ResetSnapshot):
    print("| 状态 | 过期时间（北京时间） |")
    print("|---|---|")
    rows = public_credit_rows(snapshot)
    if not rows:
        print("| 无可显示权益 | - |")
        return
    for status, expires_at in rows:
        print(f"| {status.replace('|', '/')} | {expires_at} |")


def query(codex_bin: str = "codex") -> CrossValidationResult:
    result = default_cross_validate(codex_bin=codex_bin)
    print_credit_table(result.app_server)
    return result


def doctor(codex_bin: str = "codex") -> int:
    version = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, check=False, timeout=10)
    if version.returncode != 0:
        raise ResetTransportError("codex-unavailable", "Codex CLI is unavailable")
    result = default_cross_validate(codex_bin=codex_bin)
    selected = select_earliest_available_credit(result.app_server)
    if selected is None:
        raise ResetTransportError("no-usable-credit", "no unexpired available reset credit was found")
    print_credit_table(result.app_server)
    print("doctor: ready")
    return 0


def build_operation(
    credit: ResetCredit,
    available_count: int,
    authorize_consume: bool,
    now: Optional[int] = None,
) -> dict:
    now = epoch_now() if now is None else int(now)
    if credit.expires_at is None:
        raise ResetTransportError("invalid-credit", "the selected reset credit has no expiry")
    trigger_at = credit.expires_at - DEFAULT_BEFORE_SECONDS
    if now >= credit.expires_at:
        raise ResetTransportError("credit-expired", "the selected reset credit is already expired")
    return {
        "version": 1,
        "credit_id": credit.id,
        "credit_fingerprint": secrets.token_hex(16),
        "expires_at": credit.expires_at,
        "initial_available_count": available_count,
        "trigger_at": trigger_at,
        "fallback_at": trigger_at + DEFAULT_FALLBACK_AFTER_SECONDS,
        "source_rescue_at": trigger_at + DEFAULT_SOURCE_RESCUE_AFTER_SECONDS,
        "target_at": trigger_at + DEFAULT_TARGET_AFTER_SECONDS,
        "precheck_at": [trigger_at - offset for offset in PRECHECK_OFFSETS],
        "completed_prechecks": [],
        "authorize_consume": bool(authorize_consume),
        "status": "armed" if authorize_consume else "dry-run-armed",
        "logical_attempt": 1,
        "idempotency_key": str(uuid.uuid4()),
        "idempotency_uncertain": False,
        "next_attempt_at": trigger_at,
        "attempts": [],
        "last_outcome": None,
        "last_event": "armed",
        "created_at": now,
        "updated_at": now,
        "pid": None,
    }


def arm(codex_bin: str, authorize_consume: bool, require_available_count: int) -> int:
    with manager_lock():
        try:
            existing = read_json(PRIVATE_OPERATION_PATH)
        except ResetTransportError as exc:
            if exc.category != "not-armed":
                raise
            existing = None
        if existing and existing.get("status") == "completed" and int(existing.get("expires_at", 0)) > epoch_now():
            raise ResetTransportError("already-completed", "the current reset operation already completed")
        result = default_cross_validate(codex_bin=codex_bin)
        if result.app_server.available_count != require_available_count:
            raise ResetTransportError("unexpected-credit-count", "the available reset-credit count is not the required value")
        credit = select_earliest_available_credit(result.app_server)
        if credit is None:
            raise ResetTransportError("no-usable-credit", "no unexpired available reset credit was found")
        if existing and existing.get("credit_id") == credit.id and existing.get("status") not in TERMINAL_STATUSES:
            operation = existing
            operation["authorize_consume"] = bool(authorize_consume or operation.get("authorize_consume"))
            operation["status"] = "armed" if operation["authorize_consume"] else "dry-run-armed"
            operation["last_event"] = "rearmed"
        else:
            operation = build_operation(credit, result.app_server.available_count, authorize_consume)
        save_operation(operation)
        log_event("armed", status=operation["status"], scheduled_at=iso_beijing(operation["trigger_at"]))
        print(json.dumps(public_operation(operation), ensure_ascii=False, indent=2))
    return 0


def fixed_credit(snapshot: ResetSnapshot, operation: dict) -> Optional[ResetCredit]:
    return snapshot.credit_by_id(str(operation.get("credit_id") or ""))


def cross_validate_for_operation(operation: dict, codex_bin: str) -> CrossValidationResult:
    result = default_cross_validate(codex_bin=codex_bin)
    validate_snapshot_pair(result.app_server, result.direct_get)
    return result


def confirmed_consumed(result: CrossValidationResult, operation: dict, accepted_outcome: Optional[str] = None) -> bool:
    app_credit = fixed_credit(result.app_server, operation)
    direct_credit = fixed_credit(result.direct_get, operation)
    statuses = {credit.status for credit in (app_credit, direct_credit) if credit is not None}
    if "redeemed" in statuses and "available" not in statuses:
        return True
    absent_or_not_available = all(credit is None or credit.status != "available" for credit in (app_credit, direct_credit))
    if (
        int(operation.get("initial_available_count", -1)) == 1
        and result.app_server.available_count == 0
        and result.direct_get.available_count == 0
        and absent_or_not_available
    ):
        return True
    return accepted_outcome == "alreadyRedeemed" and absent_or_not_available


def mark_completed(operation: dict, reason: str):
    operation["status"] = "completed"
    operation["last_event"] = "confirmed-consumed"
    operation["last_outcome"] = reason
    operation["completed_at"] = epoch_now()
    operation["idempotency_uncertain"] = False
    save_operation(operation)
    log_event("confirmed-consumed", outcome=reason, status="completed")


def record_attempt(operation: dict, transport: str, outcome: str, now: int):
    operation.setdefault("attempts", []).append(
        {
            "at": now,
            "transport": transport,
            "logical_attempt": operation.get("logical_attempt", 1),
            "outcome": outcome,
        }
    )
    operation["last_outcome"] = outcome
    operation["last_event"] = "attempt-finished"


def rotate_logical_attempt(operation: dict, next_at: int):
    operation["logical_attempt"] = int(operation.get("logical_attempt", 1)) + 1
    operation["idempotency_key"] = str(uuid.uuid4())
    operation["idempotency_uncertain"] = False
    operation["next_attempt_at"] = next_at


def next_source_retry_at(operation: dict, now: int) -> int:
    trigger_at = int(operation["trigger_at"])
    for offset in SOURCE_RETRY_OFFSETS:
        candidate = trigger_at + offset
        if candidate > now:
            return candidate
    return min(now + 20, int(operation["expires_at"]) - 1)


def verify_before_write(operation: dict, codex_bin: str) -> CrossValidationResult:
    result = cross_validate_for_operation(operation, codex_bin)
    if confirmed_consumed(result, operation, operation.get("last_outcome")):
        mark_completed(operation, "verified-before-write")
        return result
    app_credit = fixed_credit(result.app_server, operation)
    direct_credit = fixed_credit(result.direct_get, operation)
    if app_credit is None or direct_credit is None:
        raise ResetTransportError("credit-unavailable", "the locked reset credit is missing")
    if app_credit.status == "redeeming" or direct_credit.status == "redeeming":
        operation["status"] = "verifying"
        operation["last_event"] = "credit-redeeming"
        operation["next_attempt_at"] = epoch_now() + 10
        save_operation(operation)
        return result
    if app_credit.status != "available" or direct_credit.status != "available":
        raise ResetTransportError("credit-unavailable", "the locked reset credit is not available")
    if app_credit.expires_at != operation.get("expires_at"):
        raise ResetTransportError("credit-changed", "the locked reset credit no longer matches the armed operation")
    operation["status"] = "ready"
    return result


def confirm_after_attempt(operation: dict, codex_bin: str, outcome: str, attempts: int = 6) -> bool:
    for index in range(attempts):
        if index:
            time.sleep(10)
        try:
            result = cross_validate_for_operation(operation, codex_bin)
        except ResetTransportError as exc:
            log_event("post-attempt-query-failed", category=exc.category, attempt=index + 1)
            continue
        if confirmed_consumed(result, operation, outcome):
            mark_completed(operation, outcome)
            return True
        credit = fixed_credit(result.app_server, operation)
        if credit is not None and credit.status == "redeeming":
            operation["status"] = "verifying"
            operation["last_event"] = "credit-redeeming"
            save_operation(operation)
            continue
        if outcome in {"nothingToReset", "noCredit"}:
            return False
    return False


def primary_attempt(operation: dict, codex_bin: str, now: int) -> str:
    verify_before_write(operation, codex_bin)
    if operation.get("status") == "completed":
        return "completed"
    if operation.get("status") == "verifying":
        return "verifying"
    operation["status"] = "attempting"
    operation["last_event"] = "primary-attempt-started"
    save_operation(operation)
    try:
        with CodexAppServerClient(codex_bin=codex_bin, timeout_seconds=15) as client:
            outcome = client.consume_credit(operation["credit_id"], operation["idempotency_key"])
    except ResetTransportError as exc:
        record_attempt(operation, "app-server", f"error:{exc.category}", now)
        operation["idempotency_uncertain"] = True
        prior_uncertain = sum(
            1 for item in operation.get("attempts", []) if str(item.get("outcome", "")).startswith("error:")
        )
        operation["next_attempt_at"] = now + (20 if prior_uncertain <= 1 else 60)
        operation["status"] = "retrying"
        save_operation(operation)
        log_event("primary-attempt-error", transport="app-server", category=exc.category)
        return "uncertain"
    record_attempt(operation, "app-server", outcome, now)
    operation["idempotency_uncertain"] = False
    operation["status"] = "verifying"
    save_operation(operation)
    log_event("primary-attempt-result", transport="app-server", outcome=outcome)
    if confirm_after_attempt(operation, codex_bin, outcome):
        return "completed"
    if outcome in {"nothingToReset", "noCredit"}:
        retry_offsets = (120, 300, 480)
        logical_attempt = int(operation.get("logical_attempt", 1))
        if logical_attempt > len(retry_offsets):
            next_at = int(operation["fallback_at"])
        else:
            next_at = int(operation["trigger_at"]) + retry_offsets[logical_attempt - 1]
        if next_at <= now:
            next_at = max(now + 30, int(operation["fallback_at"]))
        rotate_logical_attempt(operation, next_at)
        operation["status"] = "retrying"
        save_operation(operation)
        return outcome
    operation["next_attempt_at"] = now + 20
    operation["status"] = "retrying"
    save_operation(operation)
    return "unconfirmed"


def run_subprocess_fallback(operation: dict, script: Path, mode: str, now: int, codex_bin: str) -> str:
    operation["status"] = "fallback"
    operation["last_event"] = f"{mode}-started"
    save_operation(operation)
    command = [sys.executable, str(script), "--execute", "--operation-path", str(PRIVATE_OPERATION_PATH)]
    if mode == "official-tui":
        command.extend(["--codex-bin", codex_bin])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        category = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "launch-failed"
        record_attempt(operation, mode, f"error:{category}", now)
        operation["idempotency_uncertain"] = True
        operation["next_attempt_at"] = now + 20
        operation["status"] = "retrying"
        save_operation(operation)
        log_event("fallback-error", transport=mode, category=category)
        return "uncertain"
    if result.returncode == 0:
        outcome = "reset"
    elif result.returncode == 3:
        outcome = "nothingToReset"
    elif result.returncode == 4:
        outcome = "noCredit"
    else:
        outcome = f"error:exit-{result.returncode}"
    record_attempt(operation, mode, outcome, now)
    operation["idempotency_uncertain"] = result.returncode not in {0, 3, 4}
    operation["status"] = "verifying"
    if mode == "official-tui":
        operation["tui_submitted"] = True
        operation["next_attempt_at"] = max(now + 20, int(operation["source_rescue_at"]))
    elif result.returncode in {3, 4}:
        rotate_logical_attempt(operation, next_source_retry_at(operation, now))
        operation["status"] = "retrying"
    else:
        operation["next_attempt_at"] = now + 20
    save_operation(operation)
    log_event("fallback-finished", transport=mode, outcome=outcome)
    if confirm_after_attempt(operation, codex_bin, outcome):
        return "completed"
    return outcome


def perform_precheck(operation: dict, codex_bin: str, scheduled_at: int):
    result = cross_validate_for_operation(operation, codex_bin)
    if confirmed_consumed(result, operation, operation.get("last_outcome")):
        mark_completed(operation, "precheck-confirmed")
        return
    credit = fixed_credit(result.app_server, operation)
    if credit is None or credit.status != "available":
        raise ResetTransportError("precheck-failed", "the locked reset credit is not available")
    completed = set(operation.get("completed_prechecks", []))
    completed.add(scheduled_at)
    operation["completed_prechecks"] = sorted(completed)
    operation["last_event"] = "precheck-passed"
    save_operation(operation)
    log_event("precheck-passed", scheduled_at=iso_beijing(scheduled_at))


def watch(codex_bin: str = "codex", once: bool = False, sleep_fn=time.sleep) -> int:
    with manager_lock():
        operation = read_json(PRIVATE_OPERATION_PATH)
        if not operation.get("authorize_consume"):
            raise ResetTransportError("not-authorized", "the armed operation does not authorize consumption")
        if operation.get("status") in TERMINAL_STATUSES:
            return 0
        operation["pid"] = os.getpid()
        operation["last_event"] = "watcher-started"
        save_operation(operation)
        log_event("watcher-started", status=operation["status"], scheduled_at=iso_beijing(operation["trigger_at"]))
        while True:
            now = epoch_now()
            operation = read_json(PRIVATE_OPERATION_PATH)
            if operation.get("status") in TERMINAL_STATUSES:
                return 0
            if now >= int(operation["expires_at"]):
                operation["status"] = "expired"
                operation["last_event"] = "credit-expired"
                save_operation(operation)
                log_event("credit-expired", status="expired")
                return 0

            pending_prechecks = [
                int(value)
                for value in operation.get("precheck_at", [])
                if int(value) <= now and int(value) not in set(operation.get("completed_prechecks", []))
            ]
            for scheduled_at in pending_prechecks:
                try:
                    perform_precheck(operation, codex_bin, scheduled_at)
                except ResetTransportError as exc:
                    operation["last_event"] = "precheck-failed"
                    save_operation(operation)
                    log_event("precheck-failed", category=exc.category, scheduled_at=iso_beijing(scheduled_at))
                operation = read_json(PRIVATE_OPERATION_PATH)

            now = epoch_now()
            if now >= int(operation["trigger_at"]) and now >= int(operation.get("next_attempt_at", operation["trigger_at"])):
                try:
                    if now >= int(operation["source_rescue_at"]):
                        verify_before_write(operation, codex_bin)
                        if operation.get("status") not in {"completed", "verifying"}:
                            run_subprocess_fallback(operation, SOURCE_RESCUE_PATH, "source-rescue", now, codex_bin)
                    elif now >= int(operation["fallback_at"]):
                        verify_before_write(operation, codex_bin)
                        if operation.get("status") not in {"completed", "verifying"}:
                            run_subprocess_fallback(operation, TUI_FALLBACK_PATH, "official-tui", now, codex_bin)
                    else:
                        primary_attempt(operation, codex_bin, now)
                except ResetTransportError as exc:
                    operation["last_event"] = "attempt-blocked"
                    operation["next_attempt_at"] = now + 20
                    operation["status"] = "retrying"
                    save_operation(operation)
                    log_event("attempt-blocked", category=exc.category)
                operation = read_json(PRIVATE_OPERATION_PATH)
                if operation.get("status") == "completed":
                    return 0

            if now >= int(operation["target_at"]) and not operation.get("target_alerted"):
                operation["target_alerted"] = True
                operation["last_event"] = "critical-window"
                operation["next_attempt_at"] = min(now + 20, int(operation["expires_at"]) - 1)
                save_operation(operation)
                log_event("critical-window", status="retrying")

            if once:
                return 0
            future = [int(operation["expires_at"]), int(operation.get("next_attempt_at", operation["trigger_at"]))]
            future.extend(
                int(value)
                for value in operation.get("precheck_at", [])
                if int(value) not in set(operation.get("completed_prechecks", []))
            )
            next_event = min((value for value in future if value > epoch_now()), default=epoch_now() + 5)
            sleep_fn(max(1, min(30, next_event - epoch_now())))


def dry_run(codex_bin: str = "codex") -> int:
    result = default_cross_validate(codex_bin=codex_bin)
    credit = select_earliest_available_credit(result.app_server)
    if credit is None:
        raise ResetTransportError("no-usable-credit", "no unexpired available reset credit was found")
    operation = build_operation(credit, result.app_server.available_count, False)
    assert operation["trigger_at"] == credit.expires_at - 3600
    assert operation["fallback_at"] == operation["trigger_at"] + 600
    assert operation["source_rescue_at"] == operation["trigger_at"] + 720
    assert operation["target_at"] == operation["trigger_at"] + 3000
    print_credit_table(result.app_server)
    print("dry-run: query -> lock -> precheck -> primary(fake) -> fallback(fake) -> verify(fake) passed; POST requests=0")
    return 0


def print_status() -> int:
    try:
        operation = read_json(PRIVATE_OPERATION_PATH)
    except ResetTransportError as exc:
        if exc.category == "not-armed":
            print(json.dumps({"status": "not-armed"}, ensure_ascii=False, indent=2))
            return 1
        raise
    print(json.dumps(public_operation(operation), ensure_ascii=False, indent=2))
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Safely schedule one Codex reset-credit redemption")
    parser.add_argument("command", choices=("query", "doctor", "dry-run", "arm", "watch", "status"))
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--authorize-consume", action="store_true")
    parser.add_argument("--require-available-count", type=int, default=1)
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "query":
            query(args.codex_bin)
            return 0
        if args.command == "doctor":
            return doctor(args.codex_bin)
        if args.command == "dry-run":
            return dry_run(args.codex_bin)
        if args.command == "arm":
            return arm(args.codex_bin, args.authorize_consume, args.require_available_count)
        if args.command == "watch":
            return watch(args.codex_bin, once=args.once)
        return print_status()
    except ResetTransportError as exc:
        print(f"reset-credit error [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

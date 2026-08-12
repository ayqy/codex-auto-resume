#!/usr/bin/env python3
"""Safe transports and normalized models for Codex reset credits.

The app-server transport is the only primary write path.  The direct WHAM
transport is read-only here so a bug in the watcher cannot accidentally turn a
status probe into a redemption request.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo


CHATGPT_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
KNOWN_OUTCOMES = {"reset", "nothingToReset", "noCredit", "alreadyRedeemed"}


class ResetTransportError(RuntimeError):
    """A deliberately sanitized transport failure."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ResetCredit:
    id: str
    reset_type: str
    status: str
    granted_at: Optional[int]
    expires_at: Optional[int]
    title: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class ResetSnapshot:
    available_count: int
    credits: tuple[ResetCredit, ...]
    source: str

    def credit_by_id(self, credit_id: str) -> Optional[ResetCredit]:
        return next((credit for credit in self.credits if credit.id == credit_id), None)


@dataclass(frozen=True)
class CrossValidationResult:
    app_server: ResetSnapshot
    direct_get: ResetSnapshot


def _timestamp(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ResetTransportError("invalid-response", "reset credit timestamp is invalid")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ResetTransportError("invalid-response", "reset credit timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise ResetTransportError("invalid-response", "reset credit timestamp has no timezone")
        return int(parsed.timestamp())
    raise ResetTransportError("invalid-response", "reset credit timestamp has an unsupported type")


def _integer(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_app_server_snapshot(payload: dict) -> ResetSnapshot:
    if not isinstance(payload, dict):
        raise ResetTransportError("invalid-response", "Codex returned an invalid rate-limit response")
    summary = payload.get("rateLimitResetCredits")
    if summary is None:
        summary = {}
    if not isinstance(summary, dict):
        raise ResetTransportError("invalid-response", "Codex returned an invalid reset-credit summary")
    raw_credits = summary.get("credits")
    credits: list[ResetCredit] = []
    if raw_credits is not None:
        if not isinstance(raw_credits, list):
            raise ResetTransportError("invalid-response", "Codex returned invalid reset-credit details")
        for raw in raw_credits:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
                raise ResetTransportError("invalid-response", "Codex returned a reset credit without an identifier")
            credits.append(
                ResetCredit(
                    id=raw["id"],
                    reset_type=str(raw.get("resetType") or "unknown"),
                    status=str(raw.get("status") or "unknown"),
                    granted_at=_timestamp(raw.get("grantedAt")),
                    expires_at=_timestamp(raw.get("expiresAt")),
                    title=raw.get("title") if isinstance(raw.get("title"), str) else None,
                    description=raw.get("description") if isinstance(raw.get("description"), str) else None,
                )
            )
    return ResetSnapshot(
        available_count=max(0, _integer(summary.get("availableCount"))),
        credits=tuple(credits),
        source="app-server",
    )


def normalize_direct_snapshot(payload: dict) -> ResetSnapshot:
    if not isinstance(payload, dict):
        raise ResetTransportError("invalid-response", "WHAM returned an invalid reset-credit response")
    raw_credits = payload.get("credits", [])
    if not isinstance(raw_credits, list):
        raise ResetTransportError("invalid-response", "WHAM returned invalid reset-credit details")
    credits: list[ResetCredit] = []
    for raw in raw_credits:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ResetTransportError("invalid-response", "WHAM returned a reset credit without an identifier")
        reset_type = str(raw.get("reset_type") or "unknown")
        if reset_type == "codex_rate_limits":
            reset_type = "codexRateLimits"
        credits.append(
            ResetCredit(
                id=raw["id"],
                reset_type=reset_type,
                status=str(raw.get("status") or "unknown"),
                granted_at=_timestamp(raw.get("granted_at")),
                expires_at=_timestamp(raw.get("expires_at")),
                title=raw.get("title") if isinstance(raw.get("title"), str) else None,
                description=raw.get("description") if isinstance(raw.get("description"), str) else None,
            )
        )
    available_count = _integer(payload.get("available_count"), -1)
    if available_count < 0:
        available_count = sum(1 for credit in credits if credit.status == "available")
    return ResetSnapshot(max(0, available_count), tuple(credits), "direct-get")


class CodexAppServerClient:
    def __init__(self, codex_bin: str = "codex", timeout_seconds: float = 20.0, env: Optional[dict] = None):
        self.codex_bin = codex_bin
        self.timeout_seconds = timeout_seconds
        self.env = dict(os.environ if env is None else env)
        self.process: Optional[subprocess.Popen] = None
        self.next_id = 1
        self.pending: dict[int, queue.Queue] = {}
        self.pending_lock = threading.Lock()
        self.failure: Optional[str] = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def start(self):
        if self.process is not None:
            return self
        try:
            self.process = subprocess.Popen(
                [self.codex_bin, "app-server", "--stdio", "-c", 'model_provider="openai"'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=self.env,
            )
        except OSError as exc:
            raise ResetTransportError("codex-unavailable", "unable to start the Codex app server") from exc
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_auto_resume",
                    "title": "Codex Auto Resume",
                    "version": "1.0.0",
                }
            },
        )
        self.notify("initialized")
        return self

    def _read_loop(self):
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.failure = "invalid-json"
                    continue
                request_id = message.get("id")
                if not isinstance(request_id, int):
                    continue
                with self.pending_lock:
                    response_queue = self.pending.get(request_id)
                if response_queue is not None:
                    response_queue.put(message)
        finally:
            self.failure = self.failure or "closed"
            with self.pending_lock:
                queues = list(self.pending.values())
            for response_queue in queues:
                response_queue.put(None)

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        if self.process is None:
            self.start()
        assert self.process is not None and self.process.stdin is not None
        if self.process.poll() is not None:
            raise ResetTransportError("app-server-closed", "Codex app server is not running")
        request_id = self.next_id
        self.next_id += 1
        response_queue: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = response_queue
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            try:
                response = response_queue.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise ResetTransportError("timeout", f"Codex request timed out: {method}") from exc
            if response is None:
                raise ResetTransportError("app-server-closed", "Codex app server closed unexpectedly")
            if "error" in response:
                raw_message = str((response.get("error") or {}).get("message", ""))
                lowered = raw_message.lower()
                if "auth" in lowered:
                    category = "authentication-required"
                elif "timed out" in lowered:
                    category = "timeout"
                elif "not found" in lowered or "unknown method" in lowered:
                    category = "method-unavailable"
                elif "must not be empty" in lowered:
                    category = "invalid-request"
                else:
                    category = "rpc-error"
                raise ResetTransportError(category, f"Codex request failed: {method}")
            result = response.get("result")
            if result is None:
                return {}
            if not isinstance(result, dict):
                raise ResetTransportError("invalid-response", f"Codex returned an invalid result: {method}")
            return result
        except (BrokenPipeError, OSError) as exc:
            raise ResetTransportError("app-server-closed", "unable to write to the Codex app server") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def notify(self, method: str, params: Optional[dict] = None):
        assert self.process is not None and self.process.stdin is not None
        message = {"method": method}
        if params is not None:
            message["params"] = params
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ResetTransportError("app-server-closed", "unable to notify the Codex app server") from exc

    def refresh_auth(self):
        return self.request("account/read", {"refreshToken": True})

    def read_rate_limits(self) -> ResetSnapshot:
        return normalize_app_server_snapshot(self.request("account/rateLimits/read"))

    def consume_credit(self, credit_id: str, idempotency_key: str) -> str:
        if not credit_id or not idempotency_key:
            raise ResetTransportError("invalid-request", "credit and idempotency identifiers are required")
        result = self.request(
            "account/rateLimitResetCredit/consume",
            {"creditId": credit_id, "idempotencyKey": idempotency_key},
        )
        outcome = result.get("outcome")
        if outcome not in KNOWN_OUTCOMES:
            raise ResetTransportError("unknown-outcome", "Codex returned an unknown reset outcome")
        return str(outcome)

    def close(self):
        process = self.process
        self.process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


class WhamReadClient:
    """Read-only client.  This class intentionally has no generic request API."""

    def __init__(
        self,
        auth_path: Optional[Path] = None,
        endpoint: str = CHATGPT_RESET_CREDITS_URL,
        timeout_seconds: float = 15.0,
        refresh_callback: Optional[Callable[[], None]] = None,
        opener=None,
    ):
        self.auth_path = auth_path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "auth.json"
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.refresh_callback = refresh_callback
        self.opener = opener or urllib.request.urlopen

    def _access_token(self) -> str:
        try:
            payload = json.loads(self.auth_path.read_text(encoding="utf-8"))
            token = payload["tokens"]["access_token"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ResetTransportError("authentication-required", "Codex authentication is unavailable") from exc
        if not isinstance(token, str) or not token:
            raise ResetTransportError("authentication-required", "Codex access token is unavailable")
        return token

    def _read_once(self) -> ResetSnapshot:
        token = self._access_token()
        request = urllib.request.Request(
            self.endpoint,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                category = "unauthorized"
            elif exc.code == 429:
                category = "rate-limited"
            else:
                category = "http-error"
            raise ResetTransportError(category, f"reset-credit query failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ResetTransportError("network-error", "reset-credit query failed due to a network error") from exc
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ResetTransportError("invalid-response", "reset-credit query returned invalid JSON") from exc
        return normalize_direct_snapshot(payload)

    def read_credits(self) -> ResetSnapshot:
        try:
            return self._read_once()
        except ResetTransportError as exc:
            if exc.category != "unauthorized" or self.refresh_callback is None:
                raise
        self.refresh_callback()
        return self._read_once()


def default_cross_validate(codex_bin: str = "codex") -> CrossValidationResult:
    with CodexAppServerClient(codex_bin=codex_bin) as client:
        app_snapshot = client.read_rate_limits()

        def refresh():
            client.refresh_auth()

        direct_snapshot = WhamReadClient(refresh_callback=refresh).read_credits()
    validate_snapshot_pair(app_snapshot, direct_snapshot)
    return CrossValidationResult(app_server=app_snapshot, direct_get=direct_snapshot)


def _comparison_rows(snapshot: ResetSnapshot):
    return {
        credit.id: (credit.status, credit.reset_type, credit.granted_at, credit.expires_at)
        for credit in snapshot.credits
    }


def validate_snapshot_pair(app_snapshot: ResetSnapshot, direct_snapshot: ResetSnapshot):
    if app_snapshot.available_count != direct_snapshot.available_count:
        raise ResetTransportError("cross-validation-failed", "reset-credit counts disagree")
    if _comparison_rows(app_snapshot) != _comparison_rows(direct_snapshot):
        raise ResetTransportError("cross-validation-failed", "reset-credit details disagree")


def select_earliest_available_credit(snapshot: ResetSnapshot, now_epoch: Optional[int] = None) -> Optional[ResetCredit]:
    now_epoch = int(datetime.now().timestamp()) if now_epoch is None else int(now_epoch)
    candidates = [
        credit
        for credit in snapshot.credits
        if credit.status == "available"
        and credit.reset_type == "codexRateLimits"
        and credit.expires_at is not None
        and credit.expires_at > now_epoch
    ]
    return min(candidates, key=lambda credit: credit.expires_at or 2**63) if candidates else None


def format_beijing_time(epoch: Optional[int]) -> str:
    if epoch is None:
        return "永不过期"
    return datetime.fromtimestamp(epoch, tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S CST")


def public_credit_rows(snapshot: ResetSnapshot):
    return [(credit.status, format_beijing_time(credit.expires_at)) for credit in snapshot.credits]

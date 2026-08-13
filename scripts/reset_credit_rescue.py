#!/usr/bin/env python3
"""Last-resort reset-credit runner using the official Codex backend contract.

This file is intentionally maintained in this repository so the reset workflow
has no runtime or installation dependency on a separate OpenAI Codex checkout.
Only --execute sends POST, and every request remains bound to the card and
idempotency key already locked in the private operation state.

Contract provenance: openai/codex commit 9dd22890f5ff47e4af128c20e32b9758a61d78d2,
codex-rs/backend-client/src/client/rate_limit_resets.rs. Future Codex contract
changes must be reviewed and ported here explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


LIST_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"


class RescueError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RescueError("invalid local state") from exc
    if not isinstance(value, dict):
        raise RescueError("invalid local state")
    return value


def access_token() -> str:
    payload = load_json(Path.home() / ".codex" / "auth.json")
    token = payload.get("tokens", {}).get("access_token")
    if not isinstance(token, str) or not token:
        raise RescueError("authentication unavailable")
    return token


def request_json(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise RescueError("official backend request failed") from exc
    if not isinstance(result, dict):
        raise RescueError("official backend returned invalid data")
    return result


def find_locked_credit(snapshot: dict, credit_id: str, expires_at: int) -> dict:
    credits = snapshot.get("credits")
    if not isinstance(credits, list):
        raise RescueError("credit details unavailable")
    for credit in credits:
        if not isinstance(credit, dict) or credit.get("id") != credit_id:
            continue
        if credit.get("status") != "available" or credit.get("reset_type") != "codex_rate_limits":
            raise RescueError("locked credit is unavailable")
        raw_expiry = credit.get("expires_at")
        if not isinstance(raw_expiry, str):
            raise RescueError("locked credit expiry unavailable")
        parsed_expiry = int(datetime.fromisoformat(raw_expiry.replace("Z", "+00:00")).timestamp())
        if parsed_expiry != expires_at:
            raise RescueError("locked credit changed")
        return credit
    raise RescueError("locked credit missing")


def run(operation_path: Path, execute: bool) -> int:
    operation = load_json(operation_path)
    required = ("credit_id", "expires_at", "idempotency_key", "source_rescue_at")
    if any(not operation.get(key) for key in required):
        raise RescueError("operation state incomplete")
    token = access_token()
    snapshot = request_json("GET", LIST_URL, token)
    find_locked_credit(snapshot, operation["credit_id"], int(operation["expires_at"]))
    payload = {
        "redeem_request_id": operation["idempotency_key"],
        "credit_id": operation["credit_id"],
    }
    if not execute:
        assert set(payload) == {"redeem_request_id", "credit_id"}
        print("bundled rescue dry-run: locked credit verified; request contract valid; POST requests=0")
        return 0
    now = int(time.time())
    if not operation.get("authorize_consume"):
        raise RescueError("operation is not authorized")
    if now < int(operation["source_rescue_at"]):
        raise RescueError("source rescue window has not started")
    if now >= int(operation["expires_at"]):
        raise RescueError("locked credit expired")
    response = request_json("POST", CONSUME_URL, token, payload)
    outcome = response.get("code")
    if outcome not in {"reset", "nothing_to_reset", "no_credit", "already_redeemed"}:
        raise RescueError("unknown official backend outcome")
    print(f"bundled rescue outcome={outcome}")
    if outcome in {"reset", "already_redeemed"}:
        return 0
    if outcome == "nothing_to_reset":
        return 3
    if outcome == "no_credit":
        return 4
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-path", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args.operation_path, args.execute)
    except RescueError as exc:
        print(f"bundled rescue error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

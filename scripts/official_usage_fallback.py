#!/usr/bin/env python3
"""Drive the official Codex /usage confirmation flow through a PTY.

Dry-run always accepts the confirmation view's safe default ("No, go back").
Execute mode is allowed only for the single credit locked by the private
operation state and selects "Yes, use reset" only after all expected labels
have appeared in order.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from codex_reset_transport import ResetTransportError, default_cross_validate, validate_snapshot_pair


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
EXPECTED_USAGE = "Redeem usage limit reset"
EXPECTED_PICKER = "Usage limit resets"
EXPECTED_CONFIRMATION = "Use this reset?"
EXPECTED_YES = "Yes, use reset"
EXPECTED_NO = "No, go back"


def safe_screen(value: bytes) -> str:
    text = value.decode("utf-8", errors="ignore")
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "\n")
    return "\n".join(line for line in text.splitlines() if line.strip())[-20000:]


def canonical_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


class CodexTui:
    def __init__(self, codex_bin: str = "codex", timeout: float = 30.0):
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.master: int | None = None
        self.process: subprocess.Popen | None = None
        self.buffer = b""

    def __enter__(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        self.process = subprocess.Popen(
            [self.codex_bin, "--no-alt-screen", "-c", 'model_provider="openai"'],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        os.close(slave)
        self.master = master
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def send(self, value: bytes):
        if self.master is None:
            raise ResetTransportError("tui-closed", "official Codex TUI is not running")
        os.write(self.master, value)

    def wait_for(self, *needles: str, timeout: float | None = None) -> str:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            current = safe_screen(self.buffer)
            canonical_current = canonical_text(current)
            if all(canonical_text(needle) in canonical_current for needle in needles):
                self.buffer = b""
                return current
            if self.process is not None and self.process.poll() is not None:
                raise ResetTransportError("tui-closed", "official Codex TUI exited unexpectedly")
            readable, _, _ = select.select([self.master], [], [], 0.25)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                raise ResetTransportError("tui-closed", "official Codex TUI became unavailable") from exc
            if not chunk:
                raise ResetTransportError("tui-closed", "official Codex TUI produced no more output")
            self.buffer = (self.buffer + chunk)[-250000:]
        raise ResetTransportError("tui-timeout", "official Codex TUI did not reach the expected view")

    def wait_for_any(self, needles: tuple[str, ...], timeout: float | None = None) -> str:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < deadline:
            current = safe_screen(self.buffer)
            canonical_current = canonical_text(current)
            for needle in needles:
                if canonical_text(needle) in canonical_current:
                    self.buffer = b""
                    return needle
            if self.process is not None and self.process.poll() is not None:
                raise ResetTransportError("tui-closed", "official Codex TUI exited unexpectedly")
            readable, _, _ = select.select([self.master], [], [], 0.25)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError as exc:
                raise ResetTransportError("tui-closed", "official Codex TUI became unavailable") from exc
            if not chunk:
                raise ResetTransportError("tui-closed", "official Codex TUI produced no more output")
            self.buffer = (self.buffer + chunk)[-250000:]
        raise ResetTransportError("tui-timeout", "official Codex TUI did not report a terminal reset result")

    def close(self):
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None


def load_operation(path: Path) -> dict:
    try:
        operation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResetTransportError("invalid-state", "the reset operation cannot be read") from exc
    if not isinstance(operation, dict) or not operation.get("credit_id"):
        raise ResetTransportError("invalid-state", "the reset operation is invalid")
    return operation


def validate_locked_credit(operation: dict, codex_bin: str):
    result = default_cross_validate(codex_bin=codex_bin)
    validate_snapshot_pair(result.app_server, result.direct_get)
    app_credit = result.app_server.credit_by_id(operation["credit_id"])
    direct_credit = result.direct_get.credit_by_id(operation["credit_id"])
    if result.app_server.available_count != 1 or result.direct_get.available_count != 1:
        raise ResetTransportError("unexpected-credit-count", "official TUI fallback requires exactly one available credit")
    if app_credit is None or direct_credit is None:
        raise ResetTransportError("credit-unavailable", "the locked reset credit is missing")
    if app_credit.status != "available" or direct_credit.status != "available":
        raise ResetTransportError("credit-unavailable", "the locked reset credit is not available")
    if app_credit.expires_at != operation.get("expires_at"):
        raise ResetTransportError("credit-changed", "the locked reset credit changed")


def run_usage_flow(execute: bool, operation_path: Path, codex_bin: str = "codex") -> int:
    operation = load_operation(operation_path)
    validate_locked_credit(operation, codex_bin)
    if execute:
        now = int(time.time())
        if not operation.get("authorize_consume"):
            raise ResetTransportError("not-authorized", "the operation does not authorize consumption")
        if now < int(operation.get("fallback_at", 2**63 - 1)):
            raise ResetTransportError("too-early", "the official TUI fallback window has not started")
        if now >= int(operation.get("expires_at", 0)):
            raise ResetTransportError("credit-expired", "the locked reset credit has expired")

    with CodexTui(codex_bin=codex_bin) as tui:
        tui.wait_for("OpenAI Codex", timeout=30)
        tui.send(b"/usage\r")
        tui.wait_for(EXPECTED_USAGE, timeout=20)
        tui.send(b"\x1b[B\r")
        tui.wait_for(EXPECTED_PICKER, "usage limit reset available", "Expires", "Cancel", timeout=30)
        tui.send(b"\r")
        tui.wait_for(EXPECTED_YES, EXPECTED_NO, "Choose a different reset", "Expires", timeout=20)
        if execute:
            tui.send(b"\x1b[A\r")
            terminal = tui.wait_for_any(
                (
                    "Usage reset. Checking your remaining resets",
                    "Usage reset. You have",
                    "Your usage does not need a reset right now",
                    "No usage limit resets are available",
                    "That reset is no longer available",
                    "Couldn't reset usage. Please try again",
                ),
                timeout=30,
            )
            if terminal.startswith("Usage reset"):
                print("official /usage fallback outcome=reset")
            elif terminal.startswith("Your usage does not need"):
                print("official /usage fallback outcome=nothing_to_reset")
                return 3
            elif terminal.startswith("No usage") or terminal.startswith("That reset"):
                print("official /usage fallback outcome=no_credit")
                return 4
            else:
                raise ResetTransportError("tui-consume-failed", "official Codex TUI reported a reset failure")
        else:
            tui.send(b"\r")
            print("official /usage fallback dry-run: confirmation reached and safely cancelled; POST requests=0")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--operation-path", type=Path, required=True)
    parser.add_argument("--codex-bin", default="codex")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run_usage_flow(args.execute, args.operation_path, args.codex_bin)
    except ResetTransportError as exc:
        print(f"official /usage fallback error [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

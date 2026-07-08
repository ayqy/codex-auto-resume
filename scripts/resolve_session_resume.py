#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_EFFORT = "medium"

def get_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def default_resume_settings() -> dict:
    return {
        "model": DEFAULT_MODEL,
        "effort": DEFAULT_EFFORT,
    }


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_limit_event(payload: dict) -> bool:
    rate_limits = payload.get("rate_limits") or {}
    primary = rate_limits.get("primary") or {}
    secondary = rate_limits.get("secondary") or {}
    return primary.get("used_percent") == 100.0 or secondary.get("used_percent") == 100.0


def extract_settings_from_rollout(path: Path) -> dict | None:
    turns: list[tuple[datetime, str, str]] = []
    last_limit_at: datetime | None = None
    last_event_at: datetime | None = None

    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            event_at = parse_timestamp(obj.get("timestamp"))
            if event_at and (last_event_at is None or event_at > last_event_at):
                last_event_at = event_at

            payload = obj.get("payload") or {}
            if obj.get("type") == "turn_context":
                model = payload.get("model")
                effort = payload.get("effort")
                if event_at and model and effort:
                    turns.append((event_at, model, effort))
            elif obj.get("type") == "event_msg" and is_limit_event(payload):
                if event_at and (last_limit_at is None or event_at > last_limit_at):
                    last_limit_at = event_at

    if not turns:
        return None

    chosen_turn = turns[-1]
    if last_limit_at is not None:
        for turn in turns:
            if turn[0] <= last_limit_at:
                chosen_turn = turn
            else:
                break

    return {
        "model": chosen_turn[1],
        "effort": chosen_turn[2],
        "last_event_at": last_event_at,
    }


def resolve_resume_settings(session_id: str, codex_home: Path | None = None) -> dict:
    if not session_id:
        return default_resume_settings()

    root = (codex_home or get_codex_home()) / "sessions"
    if not root.exists():
        return default_resume_settings()

    best_match: dict | None = None
    for path in root.rglob(f"*{session_id}.jsonl"):
        parsed = extract_settings_from_rollout(path)
        if not parsed:
            continue
        parsed["path"] = str(path)
        if best_match is None:
            best_match = parsed
            continue
        current_last_event = best_match.get("last_event_at")
        challenger_last_event = parsed.get("last_event_at")
        if challenger_last_event and (current_last_event is None or challenger_last_event > current_last_event):
            best_match = parsed

    if not best_match:
        return default_resume_settings()

    return {
        "model": best_match["model"],
        "effort": best_match["effort"],
    }


def emit_shell_runtime(settings: dict) -> str:
    lines = [
        f"export CODEX_RESUME_MODEL={shlex.quote(settings['model'])}",
        f"export CODEX_RESUME_EFFORT={shlex.quote(settings['effort'])}",
    ]
    return "\n".join(lines) + "\n"


def usage() -> str:
    return "usage: resolve_session_resume.py <session_id> [--emit-shell-runtime]"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    emit_shell = False
    if "--emit-shell-runtime" in args:
        emit_shell = True
        args.remove("--emit-shell-runtime")

    if len(args) != 1:
        print(usage(), file=sys.stderr)
        return 2

    settings = resolve_resume_settings(args[0])
    if emit_shell:
        sys.stdout.write(emit_shell_runtime(settings))
    else:
        sys.stdout.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

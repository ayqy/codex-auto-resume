#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import shlex
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Optional
from zoneinfo import ZoneInfo

PRICES = {
    "gpt-5.5": {"miss": 5.00, "hit": 0.50, "output": 30.00},
    "gpt-5.4": {"miss": 2.50, "hit": 0.25, "output": 15.00},
    "gpt-5.2": {"miss": 1.75, "hit": 0.175, "output": 14.00},
    "gpt-5": {"miss": 1.25, "hit": 0.125, "output": 10.00},
    "gpt-5.3-codex": {"miss": 1.75, "hit": 0.175, "output": 14.00},
    "gpt-5.4-mini": {"miss": 0.75, "hit": 0.075, "output": 4.50},
}

MODEL_DETECTION_REGEX = re.compile(r'"model":\s*"([^"]+)"')
SESSION_ID_REGEX = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$")
SESSION_ID_LINE_REGEX = re.compile(r"session id:\s*([0-9a-f]{8}-[0-9a-f-]{27})", re.IGNORECASE)
ABSOLUTE_PATH_REGEX = re.compile(r"(/(?:[^\s\"'`<>|]|\\ )+)")
PATH_LINE_SUFFIX_REGEX = re.compile(r":\d+(?::\d+)?$")
RUN_DIR_TIMESTAMP_REGEX = re.compile(r"^(\d{8})-(\d{6})(\d{0,3})")
NOISE_MODE_REGEX = re.compile(r"^\[MODE:\s*(EXECUTE|PLAN|RESEARCH|INNOVATE|REVIEW)\]$", re.IGNORECASE)
NOISE_BARE_MODE_REGEX = re.compile(r"^(EXECUTE|PLAN|RESEARCH|INNOVATE|REVIEW)$", re.IGNORECASE)
COMMAND_ECHO_REGEX = re.compile(r"^[%$]\s+\S+")
TAG_ONLY_REGEX = re.compile(r"^<[^>]+>$")
NOISE_BLOCK_REGEXES = [
    re.compile(r"<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.DOTALL),
    re.compile(r"<environment_context>.*?</environment_context>", re.DOTALL),
    re.compile(r"<turn_aborted>.*?</turn_aborted>", re.DOTALL),
]


def session_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_usage_item(obj: dict) -> bool:
    payload = obj.get("payload", {})
    return (
        obj.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "token_count"
    )


def create_usage_dict():
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def create_session_record(session_id: str):
    record = create_usage_dict()
    record.update(
        {
            "session_id": session_id,
            "title": session_id,
            "title_priority": 0,
            "cwd": None,
            "first_event_at": None,
            "last_event_at": None,
            "models": defaultdict(create_usage_dict),
            "_activity_turns": {},
            "_activity_spans": [],
            "active_seconds": 0,
            "has_usage_in_range": False,
        }
    )
    return record


def create_session_metadata_record(session_id: str):
    return {
        "session_id": session_id,
        "title": session_id,
        "title_priority": 0,
        "cwd": None,
        "semantic_user_messages": [],
        "referenced_paths": set(),
        "launch_events": [],
        "first_seen_at": None,
        "last_seen_at": None,
        "source_file": None,
    }


def create_display_session_record(session_id: str, title: str, cwd: Optional[str], display_kind: str):
    record = create_session_record(session_id)
    record.update(
        {
            "title": title,
            "cwd": cwd,
            "display_kind": display_kind,
            "child_count": 0,
            "child_session_ids": [],
            "project_roots": [],
            "is_synthetic_parent": False,
            "_activity_spans": [],
            "active_seconds": 0,
        }
    )
    return record


def create_day_record(day: str):
    return {
        "day": day,
        "usage": create_usage_dict(),
        "models": defaultdict(create_usage_dict),
        "_activity_spans": [],
        "active_seconds": 0,
    }


def resolve_timezone(name: Optional[str]) -> ZoneInfo:
    if name:
        return ZoneInfo(name)
    local_tz = datetime.now().astimezone().tzinfo
    if isinstance(local_tz, ZoneInfo):
        return local_tz
    return ZoneInfo("Asia/Shanghai")


def parse_range(args, target_tz: ZoneInfo):
    dt_format = "%Y-%m-%d %H:%M:%S"
    has_today = bool(args.t)
    has_date = bool(args.d)
    has_recent = bool(args.r)
    has_range = bool(args.start_time or args.end_time)

    selected_modes = sum([has_today, has_date, has_recent, has_range])
    if selected_modes != 1:
        raise ValueError("只能选择一种时间范围输入方式：-t、-d、-r 或 start_time/end_time")

    if has_today:
        now_local = datetime.now(target_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return {"mode": "usage", "start_local": start_local, "end_local": end_local, "days": 1}

    if has_date:
        start_local = datetime.strptime(args.d, "%Y-%m-%d").replace(tzinfo=target_tz)
        end_local = start_local + timedelta(days=1)
        return {"mode": "usage", "start_local": start_local, "end_local": end_local, "days": 1}

    if has_recent:
        if args.n < 1:
            raise ValueError("-n 必须是大于 0 的整数")
        now_local = datetime.now(target_tz)
        end_day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_local = end_day_start - timedelta(days=args.n - 1)
        end_local = end_day_start + timedelta(days=1)
        return {"mode": "recent", "start_local": start_local, "end_local": end_local, "days": args.n}

    if not (args.start_time and args.end_time):
        raise ValueError("start_time 和 end_time 必须同时提供")

    start_local = datetime.strptime(args.start_time, dt_format).replace(tzinfo=target_tz)
    end_local = datetime.strptime(args.end_time, dt_format).replace(tzinfo=target_tz)
    return {"mode": "usage", "start_local": start_local, "end_local": end_local, "days": 1}


def derive_session_id(path: Path) -> str:
    match = SESSION_ID_REGEX.search(path.name)
    if match:
        return match.group(1)
    return path.stem


def trim_decimal(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_token_short(value: int) -> str:
    if value >= 100_000_000:
        return f"{trim_decimal(value / 100_000_000)}亿"
    if value >= 10_000_000:
        return f"{trim_decimal(value / 10_000_000)}千万"
    return f"{trim_decimal(value / 1_000_000)}百万"


def format_token_count(value: int) -> str:
    return f"{format_token_short(value)}（{value:,}）"


def activity_turn_id(payload: dict) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    return payload.get("turn_id") or payload.get("turnId")


def is_activity_event(obj: dict) -> bool:
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return False

    obj_type = obj.get("type")
    if obj_type == "event_msg":
        payload_type = payload.get("type")
        if payload_type in {"task_started", "task_complete", "user_message", "agent_message"}:
            return True
        if payload_type != "token_count":
            return False
        info = payload.get("info", {})
        if not isinstance(info, dict):
            return False
        last_usage = info.get("last_token_usage", {})
        return isinstance(last_usage, dict) and bool(last_usage)

    if obj_type == "turn_context":
        return True

    if obj_type != "response_item":
        return False
    payload_type = payload.get("type")
    if payload_type in {"reasoning", "function_call", "function_call_output"}:
        return True
    return payload_type == "message" and payload.get("role") == "assistant"


def create_turn_activity_record():
    return {
        "started_at": None,
        "completed_at": None,
        "last_progress_at": None,
    }


def ensure_turn_activity(session_record: dict, turn_id: str):
    return session_record["_activity_turns"].setdefault(turn_id, create_turn_activity_record())


def update_turn_start(turn_record: dict, event_time_local: datetime):
    started_at = turn_record["started_at"]
    if started_at is None or event_time_local < started_at:
        turn_record["started_at"] = event_time_local


def update_turn_progress(turn_record: dict, event_time_local: datetime):
    last_progress_at = turn_record["last_progress_at"]
    if last_progress_at is None or event_time_local > last_progress_at:
        turn_record["last_progress_at"] = event_time_local


def update_turn_complete(turn_record: dict, event_time_local: datetime):
    completed_at = turn_record["completed_at"]
    if completed_at is None or event_time_local > completed_at:
        turn_record["completed_at"] = event_time_local


def register_activity_event(session_record: dict, file_state: dict, obj: dict, event_time_local: Optional[datetime]) -> None:
    if event_time_local is None or not is_activity_event(obj):
        return

    payload = obj.get("payload", {})
    obj_type = obj.get("type")
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    turn_id = activity_turn_id(payload) or file_state.get("current_turn_id")

    if obj_type == "event_msg" and payload_type == "task_started":
        if turn_id is None:
            return
        turn_record = ensure_turn_activity(session_record, turn_id)
        update_turn_start(turn_record, event_time_local)
        file_state["current_turn_id"] = turn_id
        return

    if obj_type == "turn_context":
        if turn_id is None:
            turn_id = f"synthetic:{len(session_record['_activity_turns']) + 1}"
        turn_record = ensure_turn_activity(session_record, turn_id)
        update_turn_start(turn_record, event_time_local)
        file_state["current_turn_id"] = turn_id
        return

    if obj_type == "event_msg" and payload_type == "user_message" and turn_id is None:
        turn_id = f"synthetic:{len(session_record['_activity_turns']) + 1}"
        turn_record = ensure_turn_activity(session_record, turn_id)
        update_turn_start(turn_record, event_time_local)
        file_state["current_turn_id"] = turn_id
        return

    if turn_id is None:
        return

    turn_record = ensure_turn_activity(session_record, turn_id)
    if turn_record["started_at"] is None:
        update_turn_start(turn_record, event_time_local)

    if obj_type == "event_msg" and payload_type == "task_complete":
        update_turn_complete(turn_record, event_time_local)
        update_turn_progress(turn_record, event_time_local)
        if file_state.get("current_turn_id") == turn_id:
            file_state["current_turn_id"] = None
        return

    if obj_type == "event_msg" and payload_type == "agent_message":
        update_turn_progress(turn_record, event_time_local)
        return

    if obj_type == "event_msg" and payload_type == "token_count":
        update_turn_progress(turn_record, event_time_local)
        return

    if obj_type == "response_item":
        update_turn_progress(turn_record, event_time_local)


def build_activity_spans(turns: dict[str, dict]) -> list[tuple[datetime, datetime]]:
    spans = []
    for turn in turns.values():
        start_at = turn.get("started_at")
        end_candidates = [value for value in (turn.get("last_progress_at"), turn.get("completed_at")) if value is not None]
        if start_at is None or not end_candidates:
            continue
        end_at = max(end_candidates)
        if end_at <= start_at:
            continue
        spans.append((start_at, end_at))
    return merge_activity_spans(spans)


def merge_activity_spans(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not spans:
        return []

    merged = []
    for start_at, end_at in sorted(spans, key=lambda item: (item[0], item[1])):
        if end_at <= start_at:
            continue
        if not merged or start_at > merged[-1][1]:
            merged.append((start_at, end_at))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end_at))
    return merged


def clamp_activity_spans(
    spans: list[tuple[datetime, datetime]],
    start_at: Optional[datetime],
    end_at: Optional[datetime],
) -> list[tuple[datetime, datetime]]:
    clamped = []
    for span_start, span_end in merge_activity_spans(spans):
        bounded_start = max(span_start, start_at) if start_at is not None else span_start
        bounded_end = min(span_end, end_at) if end_at is not None else span_end
        if bounded_end <= bounded_start:
            continue
        clamped.append((bounded_start, bounded_end))
    return clamped


def sum_activity_seconds(
    spans: list[tuple[datetime, datetime]],
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
) -> int:
    total_seconds = 0
    for span_start, span_end in clamp_activity_spans(spans, start_at, end_at):
        total_seconds += int((span_end - span_start).total_seconds())
    return total_seconds


def split_activity_spans_by_day(
    spans: list[tuple[datetime, datetime]],
    start_local: datetime,
    end_local: datetime,
) -> dict[str, int]:
    day_totals = {}
    day_cursor = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
    while day_cursor < end_local:
        day_end = day_cursor + timedelta(days=1)
        range_start = max(day_cursor, start_local)
        range_end = min(day_end, end_local)
        seconds = sum_activity_seconds(spans, range_start, range_end)
        if seconds > 0:
            day_totals[day_cursor.strftime("%Y-%m-%d")] = seconds
        day_cursor = day_end
    return day_totals


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0分钟"
    rounded_minutes = max(int(round(seconds / 60)), 1)
    hours, minutes = divmod(rounded_minutes, 60)
    if hours <= 0:
        return f"{rounded_minutes}分钟"
    if minutes == 0:
        return f"{hours}小时"
    return f"{hours}小时{minutes}分钟"


def usage_total(usage: dict) -> int:
    return usage["input_tokens"] + usage["output_tokens"]


def model_hit_rate(usage: dict) -> float:
    if usage["input_tokens"] <= 0:
        return 0.0
    return usage["cached_input_tokens"] / usage["input_tokens"] * 100


def model_miss_tokens(usage: dict) -> int:
    return max(usage["input_tokens"] - usage["cached_input_tokens"], 0)


def calculate_cost(model: str, usage: dict):
    if model not in PRICES:
        return None
    price_info = PRICES[model]
    miss_cost = (model_miss_tokens(usage) / 1_000_000) * price_info["miss"]
    hit_cost = (usage["cached_input_tokens"] / 1_000_000) * price_info["hit"]
    output_cost = (usage["output_tokens"] / 1_000_000) * price_info["output"]
    total_cost = miss_cost + hit_cost + output_cost
    return {
        "miss_cost": miss_cost,
        "hit_cost": hit_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
    }


def calculate_models_cost(models: dict):
    total_cost = 0.0
    partial = False
    for model, usage in models.items():
        if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            continue
        cost_info = calculate_cost(model, usage)
        if cost_info:
            total_cost += cost_info["total_cost"]
        else:
            partial = True
    return total_cost, partial


def add_usage(target: dict, usage: dict):
    target["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    target["cached_input_tokens"] += int(usage.get("cached_input_tokens", 0) or 0)
    target["output_tokens"] += int(usage.get("output_tokens", 0) or 0)


def normalize_title_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_noise_blocks(text: str) -> str:
    cleaned = text
    for pattern in NOISE_BLOCK_REGEXES:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def is_noise_title_line(line: str) -> bool:
    cleaned = normalize_title_text(line)
    if not cleaned:
        return True
    if cleaned.startswith("# AGENTS.md instructions"):
        return True
    if NOISE_MODE_REGEX.fullmatch(cleaned):
        return True
    if NOISE_BARE_MODE_REGEX.fullmatch(cleaned):
        return True
    if COMMAND_ECHO_REGEX.match(cleaned):
        return True
    if TAG_ONLY_REGEX.fullmatch(cleaned):
        return True
    return False


def is_noise_json_text(text: str) -> bool:
    cleaned = normalize_title_text(text)
    if not cleaned.startswith("{") or not cleaned.endswith("}"):
        return False
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or not payload:
        return False
    if not all(isinstance(value, (str, int, float, bool)) or value is None for value in payload.values()):
        return False
    return set(payload.keys()) <= {"status", "ok", "message", "result", "code", "error"}


def extract_semantic_title_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned_text = strip_noise_blocks(text)
    semantic_lines = []
    for line in cleaned_text.splitlines():
        normalized_line = normalize_title_text(line)
        if is_noise_title_line(normalized_line):
            continue
        semantic_lines.append(normalized_line)
    candidate = normalize_title_text(" ".join(semantic_lines))
    if not candidate or is_noise_json_text(candidate):
        return None
    return candidate


def unique_title_parts(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, (list, tuple)) else [value]
    parts = []
    for item in raw_items:
        if not isinstance(item, str):
            continue
        normalized = normalize_title_text(item)
        if not normalized or normalized in parts:
            continue
        parts.append(normalized)
    return parts


def build_session_title(text: Any, fallback: str) -> str:
    parts = unique_title_parts(text)
    if not parts:
        return fallback
    first = parts[0]
    if len(first) >= 200 or len(parts) == 1:
        return first[:200].strip() or fallback
    combined = f"{first} | {parts[1]}"
    return combined[:200].strip() or fallback


def extract_response_item_user_text(payload: dict) -> Optional[str]:
    if payload.get("type") != "message" or payload.get("role") != "user":
        return None
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "input_text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def clean_path_candidate(candidate: str) -> str:
    cleaned = candidate.strip().replace("\\ ", " ")
    cleaned = PATH_LINE_SUFFIX_REGEX.sub("", cleaned)
    cleaned = cleaned.rstrip(".,;!?)]}")
    return cleaned


def extract_existing_paths(text: Optional[str]) -> set[str]:
    if not text:
        return set()
    results = set()
    for match in ABSOLUTE_PATH_REGEX.finditer(text):
        candidate = clean_path_candidate(match.group(1))
        if len(candidate) < 2:
            continue
        try:
            if Path(candidate).exists():
                results.add(str(Path(candidate)))
        except OSError:
            continue
    return results


def register_referenced_paths(state: dict, text: Optional[str]):
    if not text:
        return
    state["referenced_paths"].update(extract_existing_paths(text))


def parse_function_call_arguments(payload: dict) -> Optional[dict]:
    if payload.get("type") != "function_call":
        return None
    raw_args = payload.get("arguments")
    if not isinstance(raw_args, str) or not raw_args.strip():
        return None
    try:
        parsed = json.loads(raw_args)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def extract_launch_project_root(command: str) -> Optional[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) < 5:
        return None
    executable = Path(tokens[0]).name.lower()
    if not executable.startswith("python"):
        return None
    if tokens[1] != "-m" or tokens[2] != "studio" or tokens[3] not in {"growth", "run"}:
        return None
    project_root = None
    for index, token in enumerate(tokens[4:], start=4):
        if token == "--project" and index + 1 < len(tokens):
            project_root = tokens[index + 1]
            break
        if token.startswith("--project="):
            project_root = token.split("=", 1)[1]
            break
    if not project_root:
        return None
    candidate = clean_path_candidate(project_root)
    try:
        if Path(candidate).exists():
            return str(Path(candidate))
    except OSError:
        return None
    return None


def register_launch_event(state: dict, event_time: Optional[datetime], arguments: Optional[dict]):
    if event_time is None or not isinstance(arguments, dict):
        return
    command = arguments.get("cmd")
    if not isinstance(command, str):
        return
    project_root = extract_launch_project_root(command)
    if not project_root:
        return
    event = {"project_root": project_root, "time": event_time, "cmd": command}
    for existing in state["launch_events"]:
        if (
            existing["project_root"] == event["project_root"]
            and existing["time"] == event["time"]
            and existing["cmd"] == event["cmd"]
        ):
            return
    state["launch_events"].append(event)


def create_file_state(file_path: Path):
    session_id = derive_session_id(file_path)
    return {
        "session_id": session_id,
        "turn_default_model": "unknown",
        "last_model_seen": "unknown",
        "current_turn_id": None,
        "cwd": None,
        "title": None,
        "semantic_user_messages": [],
        "referenced_paths": set(),
        "launch_events": [],
        "first_seen_at": None,
        "last_seen_at": None,
        "source_file": str(file_path),
        "used_in_range": False,
    }


def register_semantic_user_message(state: dict, text: Optional[str]):
    semantic_text = extract_semantic_title_text(text)
    if not semantic_text:
        return
    if semantic_text in state["semantic_user_messages"]:
        return
    if len(state["semantic_user_messages"]) >= 2:
        return
    state["semantic_user_messages"].append(semantic_text)


def merge_time_window(current: Optional[datetime], candidate: Optional[datetime], prefer_earliest: bool) -> Optional[datetime]:
    if current is None:
        return candidate
    if candidate is None:
        return current
    if prefer_earliest:
        return candidate if candidate < current else current
    return candidate if candidate > current else current


def update_file_state_metadata(obj: dict, state: dict, event_time_local: Optional[datetime]):
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return

    state["first_seen_at"] = merge_time_window(state["first_seen_at"], event_time_local, True)
    state["last_seen_at"] = merge_time_window(state["last_seen_at"], event_time_local, False)

    obj_type = obj.get("type")
    if obj_type == "session_meta":
        state["session_id"] = payload.get("session_id") or payload.get("id") or state["session_id"]

    if obj_type in ("session_meta", "turn_context"):
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            state["cwd"] = cwd
        if not state["title"]:
            semantic_title = extract_semantic_title_text(payload.get("title"))
            if semantic_title:
                state["title"] = semantic_title
        register_semantic_user_message(state, payload.get("first_user_message"))
        register_referenced_paths(state, payload.get("first_user_message"))

    if obj_type == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        register_semantic_user_message(state, message)
        register_referenced_paths(state, message)

    if obj_type == "response_item":
        user_text = extract_response_item_user_text(payload)
        register_semantic_user_message(state, user_text)
        register_referenced_paths(state, user_text)

        call_args = parse_function_call_arguments(payload)
        if call_args:
            register_referenced_paths(state, json.dumps(call_args, ensure_ascii=False))
            register_launch_event(state, event_time_local, call_args)

        if payload.get("type") == "function_call_output":
            output_value = payload.get("output")
            if isinstance(output_value, str):
                register_referenced_paths(state, output_value)


def update_model_tracking(obj: dict, state: dict):
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return

    obj_type = obj.get("type")
    if obj_type == "turn_context":
        model = payload.get("model")
        if model:
            state["turn_default_model"] = model
            state["last_model_seen"] = model
        return

    if payload.get("type") != "function_call_output":
        return

    output_value = payload.get("output", "")
    if not isinstance(output_value, str):
        return
    found_models = MODEL_DETECTION_REGEX.findall(output_value)
    if found_models:
        state["last_model_seen"] = found_models[-1]


def session_title_candidate(state: dict):
    if state["semantic_user_messages"]:
        return state["semantic_user_messages"], 4
    if state["title"]:
        return state["title"], 2
    return state["session_id"], 0


def merge_session_metadata(target: dict, state: dict):
    title_source, priority = session_title_candidate(state)
    if priority >= target["title_priority"]:
        target["title"] = build_session_title(title_source, state["session_id"])
        target["title_priority"] = priority
    if state["cwd"] and not target["cwd"]:
        target["cwd"] = state["cwd"]
    for message in state["semantic_user_messages"]:
        if message in target["semantic_user_messages"]:
            continue
        if len(target["semantic_user_messages"]) >= 2:
            break
        target["semantic_user_messages"].append(message)
    target["referenced_paths"].update(state["referenced_paths"])
    for event in state["launch_events"]:
        if any(
            existing["project_root"] == event["project_root"]
            and existing["time"] == event["time"]
            and existing["cmd"] == event["cmd"]
            for existing in target["launch_events"]
        ):
            continue
        target["launch_events"].append(event)
    target["first_seen_at"] = merge_time_window(target["first_seen_at"], state["first_seen_at"], True)
    target["last_seen_at"] = merge_time_window(target["last_seen_at"], state["last_seen_at"], False)
    if state["source_file"] and not target["source_file"]:
        target["source_file"] = state["source_file"]


def apply_session_metadata(record: dict, metadata: dict):
    title_source, priority = session_title_candidate(metadata)
    if priority >= record["title_priority"]:
        record["title"] = build_session_title(title_source, metadata["session_id"])
        record["title_priority"] = priority
    if metadata["cwd"] and not record["cwd"]:
        record["cwd"] = metadata["cwd"]


def format_event_window(start_at: Optional[datetime], end_at: Optional[datetime]) -> str:
    if start_at is None or end_at is None:
        return "未知"
    start_text = start_at.strftime("%Y-%m-%d %H:%M:%S")
    end_text = end_at.strftime("%Y-%m-%d %H:%M:%S")
    if start_text == end_text:
        return start_text
    return f"{start_text} 至 {end_text}"


def format_cost_text(total_cost: float, partial: bool) -> str:
    if partial:
        return f"${total_cost:,.2f}（部分模型未计价）"
    return f"${total_cost:,.2f}"


def iter_session_files(root: Path, start_utc: datetime):
    session_files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for file_path in session_files:
        file_mtime_utc = datetime.fromtimestamp(file_path.stat().st_mtime, tz=ZoneInfo("UTC"))
        if file_mtime_utc < start_utc - timedelta(days=7):
            continue
        yield file_path


def parse_session_id_from_stderr(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = SESSION_ID_LINE_REGEX.search(text)
    if not match:
        return None
    return match.group(1)


def parse_run_dir_started_at(run_dir: Path) -> Optional[datetime]:
    match = RUN_DIR_TIMESTAMP_REGEX.match(run_dir.name)
    if not match:
        return None
    stamp = f"{match.group(1)}{match.group(2)}"
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None


def load_run_metadata(project_root: str, run_dir: Path) -> dict:
    meta_path = run_dir / "meta.json"
    meta_payload = {}
    if meta_path.exists():
        try:
            meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta_payload = {}
    started_at = None
    finished_at = None
    if isinstance(meta_payload, dict):
        started_at_raw = meta_payload.get("started_at")
        finished_at_raw = meta_payload.get("finished_at")
        if isinstance(started_at_raw, str):
            try:
                started_at = parse_iso_datetime(started_at_raw)
            except ValueError:
                started_at = None
        if isinstance(finished_at_raw, str):
            try:
                finished_at = parse_iso_datetime(finished_at_raw)
            except ValueError:
                finished_at = None
    if started_at is None:
        started_at = parse_run_dir_started_at(run_dir)
    return {
        "project_root": project_root,
        "run_dir": str(run_dir),
        "kind": meta_payload.get("kind") if isinstance(meta_payload, dict) else None,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def load_run_index(project_root: str, cache: dict[str, dict[str, dict]]):
    if project_root in cache:
        return cache[project_root]
    run_index = {}
    runs_dir = Path(project_root) / ".24h-studio" / "runs"
    if runs_dir.is_dir():
        for stderr_path in runs_dir.glob("*/stderr.log"):
            session_id = parse_session_id_from_stderr(stderr_path)
            if not session_id:
                continue
            run_index[session_id] = load_run_metadata(project_root, stderr_path.parent)
    cache[project_root] = run_index
    return run_index


def identify_child_sessions(sessions: dict, session_catalog: dict):
    child_sessions = {}
    run_index_cache: dict[str, dict[str, dict]] = {}
    for session_id in sessions:
        metadata = session_catalog.get(session_id)
        if not metadata:
            continue
        cwd = metadata.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        run_index = load_run_index(cwd, run_index_cache)
        run_meta = run_index.get(session_id)
        if not run_meta:
            continue
        child_sessions[session_id] = {
            "session_id": session_id,
            "project_root": run_meta["project_root"],
            "run_meta": run_meta,
            "metadata": metadata,
        }
    return child_sessions


def child_anchor_time(session: dict, child_info: dict) -> Optional[datetime]:
    run_meta = child_info.get("run_meta", {})
    anchor = run_meta.get("started_at")
    if anchor is not None:
        return anchor
    if session.get("first_event_at") is not None:
        return session["first_event_at"]
    metadata = child_info.get("metadata") or {}
    return metadata.get("first_seen_at")


def score_parent_candidate(parent_meta: dict, child_session: dict, child_info: dict):
    child_start = child_anchor_time(child_session, child_info)
    if child_start is None:
        return (0, None)

    project_root = child_info["project_root"]
    best_launch_delta = None
    for event in parent_meta.get("launch_events", []):
        if event["project_root"] != project_root:
            continue
        if event["time"] is None or event["time"] > child_start:
            continue
        delta = int((child_start - event["time"]).total_seconds())
        if best_launch_delta is None or delta < best_launch_delta:
            best_launch_delta = delta
    if best_launch_delta is not None:
        return (3, best_launch_delta)

    if project_root not in parent_meta.get("referenced_paths", set()):
        return (0, None)

    parent_start = parent_meta.get("first_seen_at")
    parent_end = parent_meta.get("last_seen_at")
    if parent_start is None or parent_end is None:
        return (0, None)
    if parent_start <= child_start <= parent_end:
        return (2, 0)
    if child_start > parent_end and child_start - parent_end <= timedelta(hours=6):
        return (2, int((child_start - parent_end).total_seconds()))
    return (0, None)


def assign_parent_sessions(sessions: dict, session_catalog: dict, child_sessions: dict):
    assignments = {}
    non_child_catalog = {sid: meta for sid, meta in session_catalog.items() if sid not in child_sessions}
    for child_id, child_info in child_sessions.items():
        child_session = sessions[child_id]
        candidates = []
        for parent_id, parent_meta in non_child_catalog.items():
            if parent_id == child_id:
                continue
            score, delta = score_parent_candidate(parent_meta, child_session, child_info)
            if score <= 0 or delta is None:
                continue
            candidates.append((score, delta, parent_id))
        if not candidates:
            continue
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        best_score, best_delta, best_parent_id = candidates[0]
        ambiguous = any(score == best_score and delta == best_delta for score, delta, _ in candidates[1:])
        if ambiguous:
            continue
        assignments[child_id] = best_parent_id
    return assignments


def merge_session_usage(target: dict, source: dict):
    add_usage(target, source)
    for model, usage in source["models"].items():
        add_usage(target["models"][model], usage)
    target["first_event_at"] = merge_time_window(target["first_event_at"], source.get("first_event_at"), True)
    target["last_event_at"] = merge_time_window(target["last_event_at"], source.get("last_event_at"), False)


def sorted_model_items(models: dict):
    return sorted(models.items(), key=lambda item: (item[0] not in PRICES, item[0] == "unknown", item[0]))


def build_model_parts(models: dict):
    parts = []
    for model, usage in sorted_model_items(models):
        if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            continue
        parts.append(
            f"{model}（总Token {format_token_count(usage_total(usage))}，输入 {format_token_count(usage['input_tokens'])}，输出 {format_token_count(usage['output_tokens'])}）"
        )
    return parts


def build_session_display_records(sessions: dict, session_catalog: dict, child_sessions: dict, assignments: dict):
    parent_children = defaultdict(list)
    for child_id, parent_id in assignments.items():
        parent_children[parent_id].append(child_id)

    unresolved_child_ids = {sid for sid in child_sessions if sid not in assignments}
    display_records = []

    for parent_id, child_ids in parent_children.items():
        parent_meta = session_catalog.get(parent_id, create_session_metadata_record(parent_id))
        parent_raw = sessions.get(parent_id)
        parent_title = parent_raw["title"] if parent_raw else parent_meta["title"]
        parent_cwd = parent_raw["cwd"] if parent_raw and parent_raw.get("cwd") else parent_meta.get("cwd")
        record = create_display_session_record(parent_id, parent_title, parent_cwd, "group")
        record["child_count"] = len(child_ids)
        record["child_session_ids"] = list(child_ids)
        record["project_roots"] = sorted({child_sessions[child_id]["project_root"] for child_id in child_ids})
        record["is_synthetic_parent"] = parent_raw is None
        activity_spans = []
        if parent_raw:
            merge_session_usage(record, parent_raw)
            activity_spans.extend(parent_raw.get("_activity_spans", []))
        for child_id in child_ids:
            merge_session_usage(record, sessions[child_id])
            activity_spans.extend(sessions[child_id].get("_activity_spans", []))
        record["_activity_spans"] = merge_activity_spans(activity_spans)
        record["active_seconds"] = sum_activity_seconds(record["_activity_spans"])
        display_records.append(record)

    grouped_parent_ids = set(parent_children)
    for session_id, session in sessions.items():
        if session_id in unresolved_child_ids:
            continue
        if session_id in assignments:
            continue
        if session_id in grouped_parent_ids:
            continue
        record = create_display_session_record(session_id, session["title"], session.get("cwd"), "single")
        merge_session_usage(record, session)
        record["_activity_spans"] = list(session.get("_activity_spans", []))
        record["active_seconds"] = int(session.get("active_seconds", 0) or 0)
        display_records.append(record)

    display_records.sort(
        key=lambda item: (
            item["first_event_at"] is None,
            item["first_event_at"] or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            item["session_id"],
        )
    )
    return display_records


def percentile_90(values: list[int]) -> int:
    if not values:
        return 0
    sorted_values = sorted(values)
    index = max(math.ceil(len(sorted_values) * 0.9) - 1, 0)
    return sorted_values[index]


def build_unresolved_child_clusters(sessions: dict, child_sessions: dict, assignments: dict):
    clusters = {}
    for child_id, child_info in child_sessions.items():
        if child_id in assignments:
            continue
        session = sessions[child_id]
        cwd = session.get("cwd") or child_info["project_root"]
        cluster = clusters.setdefault(
            cwd,
            {
                "cwd": cwd,
                "session_count": 0,
                "session_ids": [],
                "usage": create_usage_dict(),
                "models": defaultdict(create_usage_dict),
                "first_event_at": None,
                "last_event_at": None,
                "totals": [],
                "active_totals": [],
                "_activity_spans": [],
            },
        )
        cluster["session_count"] += 1
        cluster["session_ids"].append(child_id)
        add_usage(cluster["usage"], session)
        for model, usage in session["models"].items():
            add_usage(cluster["models"][model], usage)
        cluster["first_event_at"] = merge_time_window(cluster["first_event_at"], session.get("first_event_at"), True)
        cluster["last_event_at"] = merge_time_window(cluster["last_event_at"], session.get("last_event_at"), False)
        cluster["totals"].append(usage_total(session))
        cluster["active_totals"].append(int(session.get("active_seconds", 0) or 0))
        cluster["_activity_spans"].extend(session.get("_activity_spans", []))

    results = []
    for cluster in clusters.values():
        totals = cluster.pop("totals")
        active_totals = cluster.pop("active_totals")
        cluster["_activity_spans"] = merge_activity_spans(cluster["_activity_spans"])
        total_cost, partial_cost = calculate_models_cost(cluster["models"])
        cluster["average_tokens"] = sum(totals) // len(totals) if totals else 0
        cluster["median_tokens"] = int(median(totals)) if totals else 0
        cluster["p90_tokens"] = percentile_90(totals)
        cluster["max_tokens"] = max(totals) if totals else 0
        cluster["active_seconds"] = sum_activity_seconds(cluster["_activity_spans"])
        cluster["average_active_seconds"] = sum(active_totals) // len(active_totals) if active_totals else 0
        cluster["median_active_seconds"] = int(median(active_totals)) if active_totals else 0
        cluster["p90_active_seconds"] = percentile_90(active_totals)
        cluster["max_active_seconds"] = max(active_totals) if active_totals else 0
        cluster["total_cost"] = total_cost
        cluster["partial_cost"] = partial_cost
        results.append(cluster)

    results.sort(
        key=lambda item: (
            item["first_event_at"] is None,
            item["first_event_at"] or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            item["cwd"],
        )
    )
    return results


def collect_usage_data(start_local: datetime, end_local: datetime, include_sessions: bool, include_days: bool):
    utc_tz = ZoneInfo("UTC")
    start_utc = start_local.astimezone(utc_tz)
    end_utc = end_local.astimezone(utc_tz)
    target_tz = start_local.tzinfo or ZoneInfo("Asia/Shanghai")

    root = session_root()
    if not root.is_dir():
        raise FileNotFoundError(f"session directory not found at {root}")

    model_totals = defaultdict(create_usage_dict)
    sessions = {} if include_sessions else None
    activity_sessions = sessions if include_sessions else {}
    days = {} if include_days else None
    session_catalog = {} if include_sessions else None

    for file_path in iter_session_files(root, start_utc):
        file_state = create_file_state(file_path)

        for obj in read_jsonl(file_path):
            timestamp_str = obj.get("timestamp")
            event_time_utc = None
            event_time_local = None
            if isinstance(timestamp_str, str):
                try:
                    event_time_utc = parse_iso_datetime(timestamp_str)
                    event_time_local = event_time_utc.astimezone(target_tz)
                except ValueError:
                    event_time_utc = None
                    event_time_local = None

            if include_sessions:
                update_file_state_metadata(obj, file_state, event_time_local)
            update_model_tracking(obj, file_state)

            if event_time_utc is None:
                continue
            if is_activity_event(obj):
                session_id = file_state["session_id"]
                activity_record = activity_sessions.setdefault(session_id, create_session_record(session_id))
                register_activity_event(activity_record, file_state, obj, event_time_local)
                if start_utc <= event_time_utc < end_utc:
                    activity_record["first_event_at"] = merge_time_window(
                        activity_record["first_event_at"], event_time_local, True
                    )
                    activity_record["last_event_at"] = merge_time_window(
                        activity_record["last_event_at"], event_time_local, False
                    )
            if not (start_utc <= event_time_utc < end_utc):
                continue
            if not is_usage_item(obj):
                continue

            payload = obj.get("payload", {})
            info = payload.get("info", {})
            if not isinstance(info, dict):
                continue
            last_usage = info.get("last_token_usage", {})
            if not isinstance(last_usage, dict) or not last_usage:
                continue

            session_id = file_state["session_id"]
            model_key = file_state["last_model_seen"] or file_state["turn_default_model"] or "unknown"
            event_time_local = event_time_utc.astimezone(target_tz)

            add_usage(model_totals[model_key], last_usage)

            session_record = activity_sessions.setdefault(session_id, create_session_record(session_id))
            if include_sessions:
                add_usage(session_record, last_usage)
                add_usage(session_record["models"][model_key], last_usage)
            session_record["first_event_at"] = merge_time_window(session_record["first_event_at"], event_time_local, True)
            session_record["last_event_at"] = merge_time_window(session_record["last_event_at"], event_time_local, False)
            session_record["has_usage_in_range"] = True

            if include_days:
                day_key = event_time_local.strftime("%Y-%m-%d")
                day_record = days.setdefault(day_key, create_day_record(day_key))
                add_usage(day_record["usage"], last_usage)
                add_usage(day_record["models"][model_key], last_usage)

            file_state["used_in_range"] = True
            file_state["last_model_seen"] = file_state["turn_default_model"]

        if include_sessions:
            metadata_record = session_catalog.setdefault(file_state["session_id"], create_session_metadata_record(file_state["session_id"]))
            merge_session_metadata(metadata_record, file_state)
            if file_state["session_id"] in sessions:
                apply_session_metadata(sessions[file_state["session_id"]], metadata_record)

    all_activity_spans = []
    for session_id, session_record in list(activity_sessions.items()):
        raw_spans = build_activity_spans(session_record["_activity_turns"])
        clamped_spans = clamp_activity_spans(raw_spans, start_local, end_local)
        session_record["_activity_spans"] = clamped_spans
        session_record["active_seconds"] = sum_activity_seconds(clamped_spans)
        if clamped_spans and session_record["first_event_at"] is None:
            session_record["first_event_at"] = clamped_spans[0][0]
            session_record["last_event_at"] = clamped_spans[-1][1]
        all_activity_spans.extend(clamped_spans)
        session_record.pop("_activity_turns", None)

        if not include_sessions:
            continue
        if usage_total(session_record) == 0 and session_record["active_seconds"] == 0:
            sessions.pop(session_id, None)

    merged_activity_spans = merge_activity_spans(all_activity_spans)
    result = {"models": model_totals, "active_seconds": sum_activity_seconds(merged_activity_spans)}
    if include_sessions:
        child_sessions = identify_child_sessions(sessions, session_catalog)
        assignments = assign_parent_sessions(sessions, session_catalog, child_sessions)
        result["sessions"] = sessions
        result["session_display"] = build_session_display_records(sessions, session_catalog, child_sessions, assignments)
        result["session_clusters"] = build_unresolved_child_clusters(sessions, child_sessions, assignments)
    if include_days:
        for day_key, seconds in split_activity_spans_by_day(merged_activity_spans, start_local, end_local).items():
            day_record = days.setdefault(day_key, create_day_record(day_key))
            day_record["active_seconds"] = seconds
        result["days"] = days
    return result


def collect_usage_report(start_local: datetime, end_local: datetime):
    return collect_usage_data(start_local, end_local, include_sessions=True, include_days=False)


def collect_usage(start_local: datetime, end_local: datetime):
    return collect_usage_report(start_local, end_local)["models"]


def collect_recent_usage(start_local: datetime, end_local: datetime, day_count: int):
    report = collect_usage_data(start_local, end_local, include_sessions=False, include_days=True)
    records = []
    for offset in range(day_count):
        day_start = start_local + timedelta(days=offset)
        day_key = day_start.strftime("%Y-%m-%d")
        day_record = report["days"].get(day_key, create_day_record(day_key))
        total_cost, partial_cost = calculate_models_cost(day_record["models"])
        records.append(
            {
                "day": day_key,
                "usage": day_record["usage"],
                "models": day_record["models"],
                "total_cost": total_cost,
                "partial_cost": partial_cost,
                "active_seconds": int(day_record.get("active_seconds", 0) or 0),
            }
        )
    return {
        "start_local": start_local,
        "end_local": end_local,
        "days": records,
        "active_seconds": int(report.get("active_seconds", 0) or 0),
    }


def build_summary_lines(report: dict, start_local: datetime, end_local: datetime):
    total_usage = create_usage_dict()
    for _, usage in report["models"].items():
        add_usage(total_usage, usage)
    total_cost, partial_cost = calculate_models_cost(report["models"])
    total_active_seconds = int(report.get("active_seconds", 0) or 0)

    lines = [
        "统计范围",
        f"{start_local.isoformat()} 至 {end_local.isoformat()}",
        "",
        "一、总体汇总",
        f"日期：{start_local.strftime('%Y-%m-%d')}",
        f"总Token：{format_token_count(usage_total(total_usage))}",
        f"输入：{format_token_count(total_usage['input_tokens'])}",
        f"缓存命中：{format_token_count(total_usage['cached_input_tokens'])}",
        f"非缓存输入：{format_token_count(model_miss_tokens(total_usage))}",
        f"输出：{format_token_count(total_usage['output_tokens'])}",
        f"活跃时长：{format_duration(total_active_seconds)}",
        f"估算总成本：{format_cost_text(total_cost, partial_cost)}",
        "",
        "二、模型汇总",
    ]

    model_items = sorted_model_items(report["models"])
    if not model_items:
        lines.append("当日未发现 token 使用记录。")
        return lines

    visible_index = 0
    for model, usage in model_items:
        if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            continue
        visible_index += 1
        cost_info = calculate_cost(model, usage)
        cost_text = f"${cost_info['total_cost']:,.2f}" if cost_info else "未计价"
        lines.extend(
            [
                f"2.{visible_index} {model}",
                f"总Token：{format_token_count(usage_total(usage))}",
                f"输入：{format_token_count(usage['input_tokens'])}",
                f"缓存命中：{format_token_count(usage['cached_input_tokens'])}",
                f"非缓存输入：{format_token_count(model_miss_tokens(usage))}",
                f"输出：{format_token_count(usage['output_tokens'])}",
                f"命中率：{model_hit_rate(usage):.2f}%",
                f"估算成本：{cost_text}",
                "",
            ]
        )

    if lines[-1] == "":
        lines.pop()
    return lines


def build_session_lines(report: dict):
    lines = ["", "三、Session 明细"]
    session_items = report.get("session_display", [])
    if not session_items:
        lines.append("当日未发现可展开的 session 级使用记录。")
    else:
        for index, session in enumerate(session_items, start=1):
            session_cost, partial_cost = calculate_models_cost(session["models"])
            model_parts = build_model_parts(session["models"])
            lines.extend(
                [
                    f"3.{index} {session['title']}",
                    f"session_id：{session['session_id']}",
                    f"时间：{format_event_window(session['first_event_at'], session['last_event_at'])}",
                    f"cwd：{session['cwd'] or '未知'}",
                ]
            )
            if session["display_kind"] == "group":
                lines.append(f"子会话数：{session['child_count']}")
                lines.append(f"关联项目：{'；'.join(session['project_roots']) if session['project_roots'] else '未知'}")
            lines.extend(
                [
                    f"总Token：{format_token_count(usage_total(session))}",
                    f"输入：{format_token_count(session['input_tokens'])}",
                    f"缓存命中：{format_token_count(session['cached_input_tokens'])}",
                    f"非缓存输入：{format_token_count(model_miss_tokens(session))}",
                    f"输出：{format_token_count(session['output_tokens'])}",
                    f"活跃时长：{format_duration(int(session.get('active_seconds', 0) or 0))}",
                    f"模型分布：{'；'.join(model_parts) if model_parts else '无'}",
                    f"估算成本：{format_cost_text(session_cost, partial_cost)}",
                    "",
                ]
            )

    lines.extend(["", "四、未归母子会话聚类"])
    clusters = report.get("session_clusters", [])
    if not clusters:
        lines.append("当日未发现未归母子会话聚类。")
    else:
        for index, cluster in enumerate(clusters, start=1):
            lines.extend(
                [
                    f"4.{index} {cluster['cwd']}",
                    f"子会话数：{cluster['session_count']}",
                    f"时间：{format_event_window(cluster['first_event_at'], cluster['last_event_at'])}",
                    f"总Token：{format_token_count(usage_total(cluster['usage']))}",
                    f"平均Token：{format_token_count(cluster['average_tokens'])}",
                    f"中位数Token：{format_token_count(cluster['median_tokens'])}",
                    f"P90 Token：{format_token_count(cluster['p90_tokens'])}",
                    f"最大Token：{format_token_count(cluster['max_tokens'])}",
                    f"总活跃时长：{format_duration(cluster['active_seconds'])}",
                    f"平均活跃时长：{format_duration(cluster['average_active_seconds'])}",
                    f"中位数活跃时长：{format_duration(cluster['median_active_seconds'])}",
                    f"P90 活跃时长：{format_duration(cluster['p90_active_seconds'])}",
                    f"最大活跃时长：{format_duration(cluster['max_active_seconds'])}",
                    f"模型分布：{'；'.join(build_model_parts(cluster['models'])) or '无'}",
                    f"估算成本：{format_cost_text(cluster['total_cost'], cluster['partial_cost'])}",
                    "",
                ]
            )

    if lines[-1] == "":
        lines.pop()
    return lines


def format_reports(report: dict, start_local: datetime, end_local: datetime):
    summary_lines = build_summary_lines(report, start_local, end_local)
    detail_lines = summary_lines + build_session_lines(report)
    return "\n".join(summary_lines), "\n".join(detail_lines) + "\n"


def build_recent_summary_lines(recent_report: dict):
    day_records = recent_report["days"]
    totals = [usage_total(record["usage"]) for record in day_records]
    active_totals = [int(record.get("active_seconds", 0) or 0) for record in day_records]
    total_tokens = sum(totals)
    total_cost = sum(record["total_cost"] for record in day_records)
    total_active_seconds = sum(active_totals)
    partial_cost = any(record["partial_cost"] for record in day_records)
    active_days = sum(1 for total in active_totals if total > 0)
    average_tokens = total_tokens // len(day_records) if day_records else 0
    average_cost = total_cost / len(day_records) if day_records else 0.0
    average_active_seconds = total_active_seconds // len(day_records) if day_records else 0
    median_tokens = int(median(totals)) if totals else 0
    median_active_seconds = int(median(active_totals)) if active_totals else 0
    p90_tokens = percentile_90(totals)
    p90_active_seconds = percentile_90(active_totals)
    max_record = max(day_records, key=lambda item: usage_total(item["usage"]), default=None)
    max_tokens = usage_total(max_record["usage"]) if max_record else 0
    max_day = max_record["day"] if max_record else "未知"
    max_cost_record = max(day_records, key=lambda item: item["total_cost"], default=None)
    max_cost = max_cost_record["total_cost"] if max_cost_record else 0.0
    max_cost_day = max_cost_record["day"] if max_cost_record else "未知"
    max_active_record = max(day_records, key=lambda item: int(item.get("active_seconds", 0) or 0), default=None)
    max_active_seconds = int(max_active_record.get("active_seconds", 0) or 0) if max_active_record else 0
    max_active_day = max_active_record["day"] if max_active_record else "未知"

    return [
        "统计范围",
        f"{recent_report['start_local'].isoformat()} 至 {recent_report['end_local'].isoformat()}",
        "",
        "一、统计",
        f"天数：{len(day_records)}",
        f"活跃天数：{active_days}",
        f"总Token：{format_token_count(total_tokens)}",
        f"日均Token：{format_token_count(average_tokens)}",
        f"中位数Token：{format_token_count(median_tokens)}",
        f"P90 Token：{format_token_count(p90_tokens)}",
        f"最大单日Token：{format_token_count(max_tokens)}（{max_day}）",
        f"总成本：{format_cost_text(total_cost, partial_cost)}",
        f"日均成本：${average_cost:,.2f}",
        f"最大单日成本：${max_cost:,.2f}（{max_cost_day}）",
        f"总活跃时长：{format_duration(total_active_seconds)}",
        f"日均活跃时长：{format_duration(average_active_seconds)}",
        f"中位数活跃时长：{format_duration(median_active_seconds)}",
        f"P90 活跃时长：{format_duration(p90_active_seconds)}",
        f"最大单日活跃时长：{format_duration(max_active_seconds)}（{max_active_day}）",
    ]


def build_recent_detail_lines(recent_report: dict):
    lines = ["", "二、每日明细"]
    for record in recent_report["days"]:
        lines.append(
            f"{record['day']} | 总Token {format_token_count(usage_total(record['usage']))} | 成本 {format_cost_text(record['total_cost'], record['partial_cost'])} | 活跃时长 {format_duration(int(record.get('active_seconds', 0) or 0))}"
        )
    return lines


def build_recent_markdown(recent_report: dict):
    summary_lines = build_recent_summary_lines(recent_report)
    detail_lines = [
        "# 最近 Token、成本与活跃时长明细",
        "",
        "## 统计摘要",
    ]
    for line in summary_lines:
        if line == "":
            continue
        if line in {"统计范围", "一、统计"}:
            detail_lines.append(f"- {line}")
        elif " 至 " in line and summary_lines[0] == "统计范围":
            detail_lines.append(f"- {line}")
        elif "：" in line:
            detail_lines.append(f"- {line}")
    detail_lines.extend(
        [
            "",
            "## 每日明细",
            "",
            "| 日期 | 总Token | 成本 | 活跃时长 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for record in recent_report["days"]:
        detail_lines.append(
            f"| {record['day']} | {format_token_count(usage_total(record['usage']))} | {format_cost_text(record['total_cost'], record['partial_cost'])} | {format_duration(int(record.get('active_seconds', 0) or 0))} |"
        )
    return "\n".join(detail_lines) + "\n"


def format_recent_reports(recent_report: dict):
    summary_lines = build_recent_summary_lines(recent_report)
    detail_lines = summary_lines + build_recent_detail_lines(recent_report)
    return "\n".join(summary_lines), "\n".join(detail_lines) + "\n", build_recent_markdown(recent_report)


def main():
    parser = argparse.ArgumentParser(description="Calculate Codex token usage and cost by model.")
    parser.add_argument("start_time", nargs="?", help="Start time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("end_time", nargs="?", help="End time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("-t", "--today", dest="t", action="store_true", help="Use the current local day")
    parser.add_argument("-d", "--date", dest="d", help="Use one local day, e.g. 2026-07-03")
    parser.add_argument("-r", dest="r", action="store_true", help="Use the latest N local days")
    parser.add_argument("-n", dest="n", type=int, default=30, help="Recent day count for -r")
    parser.add_argument("-z", "--tz", dest="z", help="Override local timezone, e.g. Asia/Shanghai")
    parser.add_argument("-s", "--summary-only", dest="s", action="store_true", help="Only print summary to stdout")
    parser.add_argument("-f", "--detail-file", dest="f", help="Write detail report to this file")
    args = parser.parse_args()

    try:
        target_tz = resolve_timezone(args.z)
        request = parse_range(args, target_tz)
        if request["mode"] == "recent":
            recent_report = collect_recent_usage(request["start_local"], request["end_local"], request["days"])
            summary_text, detail_text, file_text = format_recent_reports(recent_report)
            detail_suffix = ".md"
        else:
            report = collect_usage_report(request["start_local"], request["end_local"])
            summary_text, detail_text = format_reports(report, request["start_local"], request["end_local"])
            file_text = detail_text
            detail_suffix = ".txt"
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    detail_file = args.f
    if not detail_file:
        prefix = "codex-recent-usage." if request["mode"] == "recent" else "codex-token-usage."
        handle = tempfile.NamedTemporaryFile(
            prefix=prefix,
            suffix=detail_suffix,
            delete=False,
            dir=tempfile.gettempdir(),
        )
        detail_file = handle.name
        handle.close()

    Path(detail_file).write_text(file_text, encoding="utf-8")

    if args.s:
        print(summary_text)
        print(f"明细文件: {detail_file}")
    else:
        print(detail_text, end="")
        print(f"明细文件: {detail_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

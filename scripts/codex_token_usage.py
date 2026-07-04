#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Optional
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
SENTENCE_END_REGEX = re.compile(r"[。！？!?]")
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
        }
    )
    return record


def create_day_record(day: str):
    return {"day": day, "usage": create_usage_dict(), "models": defaultdict(create_usage_dict)}


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


def build_session_title(text: Optional[str], fallback: str) -> str:
    if not text:
        return fallback
    normalized = normalize_title_text(text)
    if not normalized:
        return fallback
    sentence_match = SENTENCE_END_REGEX.search(normalized)
    if sentence_match and sentence_match.end() <= 200:
        return normalized[: sentence_match.end()].strip() or fallback
    return normalized[:200].strip() or fallback


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


def create_file_state(file_path: Path):
    session_id = derive_session_id(file_path)
    return {
        "session_id": session_id,
        "turn_default_model": "unknown",
        "last_model_seen": "unknown",
        "cwd": None,
        "title": None,
        "semantic_user_message": None,
        "used_in_range": False,
    }


def register_semantic_user_message(state: dict, text: Optional[str]):
    if state["semantic_user_message"]:
        return
    semantic_text = extract_semantic_title_text(text)
    if semantic_text:
        state["semantic_user_message"] = semantic_text


def update_file_state_metadata(obj: dict, state: dict):
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return

    obj_type = obj.get("type")
    if obj_type == "session_meta":
        # 6/27 和 6/28 的排查显示问题在标题噪音，不在 session_id 串号。
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

    if obj_type == "event_msg" and payload.get("type") == "user_message":
        register_semantic_user_message(state, payload.get("message"))

    if obj_type == "response_item":
        register_semantic_user_message(state, extract_response_item_user_text(payload))


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
    if state["semantic_user_message"]:
        return state["semantic_user_message"], 4
    if state["title"]:
        return state["title"], 2
    return state["session_id"], 0


def apply_session_metadata(record: dict, state: dict):
    title_source, priority = session_title_candidate(state)
    if priority >= record["title_priority"]:
        record["title"] = build_session_title(title_source, state["session_id"])
        record["title_priority"] = priority
    if state["cwd"] and not record["cwd"]:
        record["cwd"] = state["cwd"]


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
        if file_mtime_utc < start_utc - timedelta(days=2):
            continue
        yield file_path


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
    days = {} if include_days else None

    for file_path in iter_session_files(root, start_utc):
        file_state = create_file_state(file_path)

        for obj in read_jsonl(file_path):
            update_file_state_metadata(obj, file_state)
            update_model_tracking(obj, file_state)

            timestamp_str = obj.get("timestamp")
            if not timestamp_str:
                continue

            try:
                event_time_utc = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except ValueError:
                continue

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

            if include_sessions:
                session_record = sessions.setdefault(session_id, create_session_record(session_id))
                add_usage(session_record, last_usage)
                add_usage(session_record["models"][model_key], last_usage)

                if session_record["first_event_at"] is None or event_time_local < session_record["first_event_at"]:
                    session_record["first_event_at"] = event_time_local
                if session_record["last_event_at"] is None or event_time_local > session_record["last_event_at"]:
                    session_record["last_event_at"] = event_time_local

            if include_days:
                day_key = event_time_local.strftime("%Y-%m-%d")
                day_record = days.setdefault(day_key, create_day_record(day_key))
                add_usage(day_record["usage"], last_usage)
                add_usage(day_record["models"][model_key], last_usage)

            file_state["used_in_range"] = True
            file_state["last_model_seen"] = file_state["turn_default_model"]

        if include_sessions and file_state["used_in_range"]:
            apply_session_metadata(sessions[file_state["session_id"]], file_state)

    result = {"models": model_totals}
    if include_sessions:
        result["sessions"] = sessions
    if include_days:
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
            }
        )
    return {"start_local": start_local, "end_local": end_local, "days": records}


def sorted_model_items(models: dict):
    return sorted(models.items(), key=lambda item: (item[0] not in PRICES, item[0] == "unknown", item[0]))


def build_summary_lines(report: dict, start_local: datetime, end_local: datetime):
    total_usage = create_usage_dict()
    for _, usage in report["models"].items():
        add_usage(total_usage, usage)
    total_cost, partial_cost = calculate_models_cost(report["models"])

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
    session_items = sorted(
        report["sessions"].values(),
        key=lambda item: (
            item["first_event_at"] is None,
            item["first_event_at"] or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            item["session_id"],
        ),
    )

    if not session_items:
        lines.append("当日未发现 session 级 token 使用记录。")
        return lines

    for index, session in enumerate(session_items, start=1):
        model_parts = []
        session_cost, partial_cost = calculate_models_cost(session["models"])

        for model, usage in sorted_model_items(session["models"]):
            model_parts.append(
                f"{model}（总Token {format_token_count(usage_total(usage))}，输入 {format_token_count(usage['input_tokens'])}，输出 {format_token_count(usage['output_tokens'])}）"
            )

        lines.extend(
            [
                f"3.{index} {session['title']}",
                f"session_id：{session['session_id']}",
                f"时间：{format_event_window(session['first_event_at'], session['last_event_at'])}",
                f"cwd：{session['cwd'] or '未知'}",
                f"总Token：{format_token_count(usage_total(session))}",
                f"输入：{format_token_count(session['input_tokens'])}",
                f"缓存命中：{format_token_count(session['cached_input_tokens'])}",
                f"非缓存输入：{format_token_count(model_miss_tokens(session))}",
                f"输出：{format_token_count(session['output_tokens'])}",
                f"模型分布：{'；'.join(model_parts) if model_parts else '无'}",
                f"估算成本：{format_cost_text(session_cost, partial_cost)}",
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


def percentile_90(values: list[int]) -> int:
    if not values:
        return 0
    sorted_values = sorted(values)
    index = max(math.ceil(len(sorted_values) * 0.9) - 1, 0)
    return sorted_values[index]


def build_recent_summary_lines(recent_report: dict):
    day_records = recent_report["days"]
    totals = [usage_total(record["usage"]) for record in day_records]
    total_tokens = sum(totals)
    total_cost = sum(record["total_cost"] for record in day_records)
    partial_cost = any(record["partial_cost"] for record in day_records)
    active_days = sum(1 for total in totals if total > 0)
    average_tokens = total_tokens // len(day_records) if day_records else 0
    average_cost = total_cost / len(day_records) if day_records else 0.0
    median_tokens = int(median(totals)) if totals else 0
    p90_tokens = percentile_90(totals)
    max_record = max(day_records, key=lambda item: usage_total(item["usage"]), default=None)
    max_tokens = usage_total(max_record["usage"]) if max_record else 0
    max_day = max_record["day"] if max_record else "未知"

    return [
        "统计范围",
        f"{recent_report['start_local'].isoformat()} 至 {recent_report['end_local'].isoformat()}",
        "",
        "一、统计",
        f"天数：{len(day_records)}",
        f"活跃天数：{active_days}",
        f"总Token：{format_token_count(total_tokens)}",
        f"总成本：{format_cost_text(total_cost, partial_cost)}",
        f"日均Token：{format_token_count(average_tokens)}",
        f"日均成本：${average_cost:,.2f}",
        f"中位数Token：{format_token_count(median_tokens)}",
        f"P90 Token：{format_token_count(p90_tokens)}",
        f"最大单日Token：{format_token_count(max_tokens)}（{max_day}）",
    ]


def build_recent_detail_lines(recent_report: dict):
    lines = ["", "二、每日明细"]
    for record in recent_report["days"]:
        lines.append(
            f"{record['day']} | 总Token {format_token_count(usage_total(record['usage']))} | 成本 {format_cost_text(record['total_cost'], record['partial_cost'])}"
        )
    return lines


def build_recent_markdown(recent_report: dict):
    summary_lines = build_recent_summary_lines(recent_report)
    detail_lines = [
        "# 最近Token与成本明细",
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
            "| 日期 | 总Token | 成本 |",
            "| --- | --- | --- |",
        ]
    )
    for record in recent_report["days"]:
        detail_lines.append(
            f"| {record['day']} | {format_token_count(usage_total(record['usage']))} | {format_cost_text(record['total_cost'], record['partial_cost'])} |"
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

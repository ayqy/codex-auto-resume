#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
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
    has_range = bool(args.start_time or args.end_time)

    selected_modes = sum([has_today, has_date, has_range])
    if selected_modes != 1:
        raise ValueError("只能选择一种时间范围输入方式：-t、-d 或 start_time/end_time")

    if has_today:
        now_local = datetime.now(target_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return start_local, end_local

    if has_date:
        start_local = datetime.strptime(args.d, "%Y-%m-%d").replace(tzinfo=target_tz)
        end_local = start_local + timedelta(days=1)
        return start_local, end_local

    if not (args.start_time and args.end_time):
        raise ValueError("start_time 和 end_time 必须同时提供")

    start_local = datetime.strptime(args.start_time, dt_format).replace(tzinfo=target_tz)
    end_local = datetime.strptime(args.end_time, dt_format).replace(tzinfo=target_tz)
    return start_local, end_local


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


def add_usage(target: dict, usage: dict):
    target["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
    target["cached_input_tokens"] += int(usage.get("cached_input_tokens", 0) or 0)
    target["output_tokens"] += int(usage.get("output_tokens", 0) or 0)


def normalize_first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            return cleaned
    return re.sub(r"\s+", " ", text).strip()


def build_session_title(text: Optional[str], fallback: str) -> str:
    if not text:
        return fallback
    first_line = normalize_first_line(text)
    if not first_line:
        return fallback
    sentence_match = SENTENCE_END_REGEX.search(first_line)
    if sentence_match and sentence_match.end() <= 200:
        return first_line[: sentence_match.end()].strip() or fallback
    return first_line[:200].strip() or fallback


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
        "first_user_message": None,
        "fallback_user_message": None,
        "used_in_range": False,
    }


def update_file_state_metadata(obj: dict, state: dict):
    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        return

    obj_type = obj.get("type")
    if obj_type == "session_meta":
        state["session_id"] = payload.get("session_id") or payload.get("id") or state["session_id"]

    if obj_type in ("session_meta", "turn_context"):
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            state["cwd"] = cwd
        if not state["title"]:
            title = payload.get("title")
            if isinstance(title, str) and title.strip():
                state["title"] = title
        if not state["first_user_message"]:
            first_user_message = payload.get("first_user_message")
            if isinstance(first_user_message, str) and first_user_message.strip():
                state["first_user_message"] = first_user_message

    if obj_type == "event_msg" and payload.get("type") == "user_message" and not state["first_user_message"]:
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            state["first_user_message"] = message

    if obj_type == "response_item" and not state["fallback_user_message"]:
        response_text = extract_response_item_user_text(payload)
        if response_text:
            state["fallback_user_message"] = response_text


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
    if state["first_user_message"]:
        return state["first_user_message"], 4
    if state["fallback_user_message"]:
        return state["fallback_user_message"], 2
    if state["title"]:
        return state["title"], 1
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


def collect_usage_report(start_local: datetime, end_local: datetime):
    utc_tz = ZoneInfo("UTC")
    start_utc = start_local.astimezone(utc_tz)
    end_utc = end_local.astimezone(utc_tz)
    target_tz = start_local.tzinfo or ZoneInfo("Asia/Shanghai")

    root = session_root()
    if not root.is_dir():
        raise FileNotFoundError(f"session directory not found at {root}")

    model_totals = defaultdict(create_usage_dict)
    sessions = {}

    session_files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for file_path in session_files:
        file_mtime_utc = datetime.fromtimestamp(file_path.stat().st_mtime, tz=utc_tz)
        if file_mtime_utc < start_utc - timedelta(days=2):
            continue

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

            session_record = sessions.setdefault(session_id, create_session_record(session_id))
            add_usage(session_record, last_usage)
            add_usage(session_record["models"][model_key], last_usage)

            if session_record["first_event_at"] is None or event_time_local < session_record["first_event_at"]:
                session_record["first_event_at"] = event_time_local
            if session_record["last_event_at"] is None or event_time_local > session_record["last_event_at"]:
                session_record["last_event_at"] = event_time_local

            file_state["used_in_range"] = True
            file_state["last_model_seen"] = file_state["turn_default_model"]

        if file_state["used_in_range"]:
            apply_session_metadata(sessions[file_state["session_id"]], file_state)

    return {"models": model_totals, "sessions": sessions}


def collect_usage(start_local: datetime, end_local: datetime):
    return collect_usage_report(start_local, end_local)["models"]


def sorted_model_items(models: dict):
    return sorted(models.items(), key=lambda item: (item[0] not in PRICES, item[0] == "unknown", item[0]))


def build_summary_lines(report: dict, start_local: datetime, end_local: datetime):
    total_usage = create_usage_dict()
    total_cost = 0.0

    for _, usage in report["models"].items():
        add_usage(total_usage, usage)

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
    ]

    for model, usage in sorted_model_items(report["models"]):
        cost_info = calculate_cost(model, usage)
        if cost_info:
            total_cost += cost_info["total_cost"]

    lines.append(f"估算总成本：${total_cost:,.2f}")
    lines.append("")
    lines.append("二、模型汇总")

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
        session_cost = 0.0
        partial_cost = False

        for model, usage in sorted_model_items(session["models"]):
            cost_info = calculate_cost(model, usage)
            if cost_info:
                session_cost += cost_info["total_cost"]
            else:
                partial_cost = True
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


def main():
    parser = argparse.ArgumentParser(description="Calculate Codex token usage and cost by model.")
    parser.add_argument("start_time", nargs="?", help="Start time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("end_time", nargs="?", help="End time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("-t", "--today", dest="t", action="store_true", help="Use the current local day")
    parser.add_argument("-d", "--date", dest="d", help="Use one local day, e.g. 2026-07-03")
    parser.add_argument("-z", "--tz", dest="z", help="Override local timezone, e.g. Asia/Shanghai")
    parser.add_argument("-s", "--summary-only", dest="s", action="store_true", help="Only print summary to stdout")
    parser.add_argument("-f", "--detail-file", dest="f", help="Write detail report to this file")
    args = parser.parse_args()

    try:
        target_tz = resolve_timezone(args.z)
        start_local, end_local = parse_range(args, target_tz)
        report = collect_usage_report(start_local, end_local)
        summary_text, detail_text = format_reports(report, start_local, end_local)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    detail_file = args.f
    if not detail_file:
        handle = tempfile.NamedTemporaryFile(
            prefix="codex-token-usage.",
            suffix=".txt",
            delete=False,
            dir=tempfile.gettempdir(),
        )
        detail_file = handle.name
        handle.close()

    Path(detail_file).write_text(detail_text, encoding="utf-8")

    if args.s:
        print(summary_text)
        print(f"明细文件: {detail_file}")
    else:
        print(detail_text, end="")
        print(f"明细文件: {detail_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

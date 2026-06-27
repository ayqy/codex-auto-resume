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


def resolve_timezone(name: Optional[str]) -> ZoneInfo:
    if name:
        return ZoneInfo(name)
    local_tz = datetime.now().astimezone().tzinfo
    if isinstance(local_tz, ZoneInfo):
        return local_tz
    return ZoneInfo("Asia/Shanghai")


def parse_range(args, target_tz: ZoneInfo):
    dt_format = "%Y-%m-%d %H:%M:%S"
    if args.today:
        now_local = datetime.now(target_tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        return start_local, end_local

    if args.start_time and args.end_time:
        start_local = datetime.strptime(args.start_time, dt_format).replace(tzinfo=target_tz)
        end_local = datetime.strptime(args.end_time, dt_format).replace(tzinfo=target_tz)
        return start_local, end_local

    raise ValueError("either --today or start_time/end_time is required")


def collect_usage(start_local: datetime, end_local: datetime):
    utc_tz = ZoneInfo("UTC")
    start_utc = start_local.astimezone(utc_tz)
    end_utc = end_local.astimezone(utc_tz)

    root = session_root()
    if not root.is_dir():
        raise FileNotFoundError(f"session directory not found at {root}")

    results = defaultdict(create_usage_dict)
    turn_default_model = "unknown"
    last_model_seen = "unknown"

    session_files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for file_path in session_files:
        file_mtime_utc = datetime.fromtimestamp(file_path.stat().st_mtime, tz=utc_tz)
        if file_mtime_utc < start_utc - timedelta(days=2):
            continue

        for obj in read_jsonl(file_path):
            timestamp_str = obj.get("timestamp")
            if not timestamp_str:
                continue

            try:
                event_time_utc = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if not (start_utc <= event_time_utc < end_utc):
                continue

            obj_type = obj.get("type")
            payload = obj.get("payload", {})
            if not isinstance(payload, dict):
                continue

            if obj_type == "turn_context":
                model = payload.get("model")
                if model:
                    turn_default_model = model
                    last_model_seen = model
                continue

            if payload.get("type") == "function_call_output":
                output_str = payload.get("output", "")
                if not isinstance(output_str, str):
                    continue
                found_models = MODEL_DETECTION_REGEX.findall(output_str)
                if found_models:
                    last_model_seen = found_models[-1]
                continue

            if is_usage_item(obj):
                info = payload.get("info", {})
                if not isinstance(info, dict):
                    continue
                last_usage = info.get("last_token_usage", {})
                if not isinstance(last_usage, dict):
                    continue
                if not last_usage:
                    continue
                model_key = last_model_seen
                results[model_key]["input_tokens"] += last_usage.get("input_tokens", 0)
                results[model_key]["cached_input_tokens"] += last_usage.get("cached_input_tokens", 0)
                results[model_key]["output_tokens"] += last_usage.get("output_tokens", 0)
                last_model_seen = turn_default_model

    return results


def format_reports(results: dict, start_local: datetime, end_local: datetime):
    grand_total_cost = 0.0
    total_input = 0
    total_cached = 0
    total_output = 0
    summary_lines = [
        f"今日 Token 使用总结 {start_local.strftime('%Y-%m-%d')}",
    ]
    detail_lines = [
        f"Processing range: {start_local.isoformat()} to {end_local.isoformat()}",
        "--- Per-Model Analysis ---",
    ]

    sorted_models = sorted(results.keys(), key=lambda x: (x not in PRICES, x == "unknown", x))
    model_summaries = []

    for model in sorted_models:
        usage = results[model]
        if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
            continue

        total_model_input = usage["input_tokens"]
        total_model_cached = usage["cached_input_tokens"]
        total_model_miss = max(total_model_input - total_model_cached, 0)
        total_model_output = usage["output_tokens"]
        hit_rate = (total_model_cached / total_model_input * 100) if total_model_input > 0 else 0.0

        total_input += total_model_input
        total_cached += total_model_cached
        total_output += total_model_output

        detail_lines.append(f"--- Model: {model} ---")
        detail_lines.append(
            "Usage: Total Input: "
            f"{total_model_input:,} (Hits: {total_model_cached:,}, Misses: {total_model_miss:,}, "
            f"Rate: {hit_rate:.2f}%) | Total Output: {total_model_output:,}"
        )

        if model in PRICES:
            price_info = PRICES[model]
            miss_cost = (total_model_miss / 1_000_000) * price_info["miss"]
            hit_cost = (total_model_cached / 1_000_000) * price_info["hit"]
            output_cost = (total_model_output / 1_000_000) * price_info["output"]
            model_total_cost = miss_cost + hit_cost + output_cost
            grand_total_cost += model_total_cost
            detail_lines.append(
                f"Cost: Model Total: ${model_total_cost:,.2f} "
                f"(Miss: ${miss_cost:,.2f}, Hit: ${hit_cost:,.2f}, Output: ${output_cost:,.2f})"
            )
            model_summaries.append(
                f"{model}: in {total_model_input:,}, cached {total_model_cached:,}, "
                f"out {total_model_output:,}, cost ${model_total_cost:,.2f}"
            )
        else:
            detail_lines.append("Cost: Not calculated (model not in price list)")
            model_summaries.append(
                f"{model}: in {total_model_input:,}, cached {total_model_cached:,}, "
                f"out {total_model_output:,}, cost N/A"
            )

    total_miss = max(total_input - total_cached, 0)
    summary_lines.append(
        f"总输入 {total_input:,} | 缓存命中 {total_cached:,} | 非缓存输入 {total_miss:,} | 输出 {total_output:,}"
    )
    summary_lines.append(f"估算总成本 ${grand_total_cost:,.2f}")
    if model_summaries:
        summary_lines.append("模型汇总:")
        summary_lines.extend(model_summaries)
    else:
        summary_lines.append("模型汇总: 今日未发现 token 使用记录")

    detail_lines.append(f"--- GRAND TOTAL COST: ${grand_total_cost:,.2f} ---")
    return "\n".join(summary_lines), "\n".join(detail_lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Calculate Codex token usage and cost by model.")
    parser.add_argument("start_time", nargs="?", help="Start time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("end_time", nargs="?", help="End time 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--today", action="store_true", help="Use the current local day")
    parser.add_argument("--tz", help="Override local timezone, e.g. Asia/Shanghai")
    parser.add_argument("--summary-only", action="store_true", help="Only print summary to stdout")
    parser.add_argument("--detail-file", help="Write detail report to this file")
    args = parser.parse_args()

    try:
        target_tz = resolve_timezone(args.tz)
        start_local, end_local = parse_range(args, target_tz)
        results = collect_usage(start_local, end_local)
        summary_text, detail_text = format_reports(results, start_local, end_local)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    detail_file = args.detail_file
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

    if args.summary_only:
        print(summary_text)
        print(f"明细文件: {detail_file}")
    else:
        print(summary_text)
        print(detail_text, end="")
        print(f"明细文件: {detail_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

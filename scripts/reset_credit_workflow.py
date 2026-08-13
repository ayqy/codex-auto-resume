#!/usr/bin/env python3
"""Interactive single-entry workflow for reset-credit redemption."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import reset_credit_manager as manager
from codex_reset_transport import ResetCredit, ResetTransportError, default_cross_validate, format_beijing_time
from install_reset_credit_launch_agent import install as install_launch_agent


CAFFEINATE_PATH = Path("/usr/bin/caffeinate")
LAUNCHCTL_PATH = Path("/bin/launchctl")
AUTH_PATH = Path.home() / ".codex" / "auth.json"
BUNDLED_RESCUE_PATH = manager.BASE_DIR / "scripts" / "reset_credit_rescue.py"
ACTIVE_STATUSES = {"armed", "ready", "attempting", "retrying", "verifying", "fallback"}


def output_json(value: dict, output_fn: Callable[[str], None]) -> None:
    output_fn(json.dumps(value, ensure_ascii=False, indent=2))


def resolve_codex_bin(explicit: Optional[str] = None) -> str:
    candidate = explicit or shutil.which("codex")
    if not candidate:
        raise ResetTransportError("codex-unavailable", "未找到 Codex CLI，请先安装并登录 Codex")
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ResetTransportError("codex-unavailable", "Codex CLI 不可执行")
    return str(path.absolute())


def validate_auth(auth_path: Optional[Path] = None) -> None:
    auth_path = auth_path or AUTH_PATH
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
        token = payload.get("tokens", {}).get("access_token")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ResetTransportError("authentication-required", "Codex 登录信息不可用，请先运行 codex 登录") from exc
    if not isinstance(token, str) or not token:
        raise ResetTransportError("authentication-required", "Codex 登录信息中没有可用凭据")


def prepare(codex_bin: Optional[str] = None, output_fn: Callable[[str], None] = print) -> str:
    output_fn("[1/5] 准备环境")
    if sys.platform != "darwin":
        raise ResetTransportError("unsupported-platform", "自动定时执行当前仅支持 macOS")
    if not LAUNCHCTL_PATH.is_file() or not os.access(LAUNCHCTL_PATH, os.X_OK):
        raise ResetTransportError("missing-system-component", "macOS 系统组件 launchctl 不可用")
    if not CAFFEINATE_PATH.is_file() or not os.access(CAFFEINATE_PATH, os.X_OK):
        raise ResetTransportError(
            "missing-system-component",
            "macOS 默认系统组件 /usr/bin/caffeinate 不可用，请先修复 macOS 系统组件",
        )
    if not BUNDLED_RESCUE_PATH.is_file():
        raise ResetTransportError("missing-rescue", "项目内置的重置兜底脚本缺失")

    resolved = resolve_codex_bin(codex_bin)
    version = subprocess.run(
        [resolved, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if version.returncode != 0:
        raise ResetTransportError("codex-unavailable", "Codex CLI 自检失败")
    validate_auth()
    output_fn(f"  ✓ {version.stdout.strip() or 'Codex CLI'}")
    output_fn("  ✓ 已登录；凭据不会输出")
    output_fn("  ✓ caffeinate 为 macOS 自带组件，无需下载安装")
    output_fn("  ✓ 官方契约兜底脚本已内置，无需外部源码目录")
    return resolved


def available_credits(snapshot, now: Optional[int] = None) -> list[ResetCredit]:
    now = manager.epoch_now() if now is None else int(now)
    return sorted(
        (
            credit
            for credit in snapshot.credits
            if credit.status == "available"
            and credit.reset_type == "codexRateLimits"
            and credit.expires_at is not None
            and credit.expires_at > now
        ),
        key=lambda credit: credit.expires_at or 2**63,
    )


def print_choices(credits: list[ResetCredit], output_fn: Callable[[str], None] = print) -> None:
    output_fn("| 编号 | 状态 | 过期时间（北京时间） | 默认定时执行 |")
    output_fn("|---:|---|---|---|")
    for index, credit in enumerate(credits, start=1):
        scheduled = (credit.expires_at or 0) - manager.DEFAULT_BEFORE_SECONDS
        output_fn(
            f"| {index} | {credit.status} | {format_beijing_time(credit.expires_at)} | "
            f"{format_beijing_time(scheduled)} |"
        )


def prompt_number(
    prompt: str,
    minimum: int,
    maximum: int,
    default: int,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    while True:
        raw = input_fn(prompt).strip()
        if not raw:
            return default
        if raw.isdigit() and minimum <= int(raw) <= maximum:
            return int(raw)
        output_fn(f"请输入 {minimum} 到 {maximum} 之间的编号。")


def choose_credit(
    credits: list[ResetCredit],
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> ResetCredit:
    print_choices(credits, output_fn)
    choice = prompt_number(
        f"选择重置权益 [1-{len(credits)}，默认 1]：",
        1,
        len(credits),
        1,
        input_fn,
        output_fn,
    )
    return credits[choice - 1]


def choose_execution_mode(
    credit: ResetCredit,
    now: Optional[int] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    now = manager.epoch_now() if now is None else int(now)
    scheduled_at = int(credit.expires_at or 0) - manager.DEFAULT_BEFORE_SECONDS
    if scheduled_at <= now:
        output_fn("权益已进入到期前一小时窗口，将立即执行。")
        return "immediate"
    output_fn("执行方式：")
    output_fn(f"  1. 定时执行（推荐）：{format_beijing_time(scheduled_at)}")
    output_fn("  2. 立即执行")
    choice = prompt_number("请选择 [1/2，默认 1]：", 1, 2, 1, input_fn, output_fn)
    return "scheduled" if choice == 1 else "immediate"


def validate_dry_run(
    credit: ResetCredit,
    available_count: int,
    execution_mode: str,
    now: Optional[int] = None,
    output_fn: Callable[[str], None] = print,
) -> dict:
    operation = manager.build_operation(
        credit,
        available_count,
        False,
        now=now,
        execution_mode=execution_mode,
    )
    assert operation["authorize_consume"] is False
    assert operation["credit_id"] == credit.id
    assert operation["trigger_at"] < operation["expires_at"]
    assert operation["trigger_at"] <= operation["fallback_at"] < operation["source_rescue_at"]
    assert operation["source_rescue_at"] < operation["target_at"] < operation["expires_at"]
    output_fn("  ✓ 查询 → 固定选卡 → 主路径 → /usage 兜底 → 内置契约兜底 → 双路确认")
    output_fn("  ✓ 演练完成：POST requests=0")
    return operation


def confirm(
    credit: ResetCredit,
    operation: dict,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> bool:
    output_fn("[4/5] 最终确认")
    output_fn(f"  权益过期：{format_beijing_time(credit.expires_at)}")
    mode = "立即执行" if operation["execution_mode"] == "immediate" else "定时执行"
    output_fn(f"  执行方式：{mode}")
    output_fn(f"  首次执行：{format_beijing_time(operation['trigger_at'])}")
    output_fn("  成功前不会切换权益；不明确重试会复用同一幂等键。")
    raw = input_fn("确认使用这张一次性权益？[y/N]：").strip().lower()
    return raw in {"y", "yes", "确认"}


def current_operation() -> Optional[dict]:
    try:
        return manager.read_json(manager.PRIVATE_OPERATION_PATH)
    except ResetTransportError as exc:
        if exc.category == "not-armed":
            return None
        raise


def wait_for_immediate(
    sleep_fn: Callable[[float], None] = time.sleep,
    output_fn: Callable[[str], None] = print,
) -> int:
    last_event = None
    while True:
        operation = manager.read_json(manager.PRIVATE_OPERATION_PATH)
        event = operation.get("last_event")
        if event != last_event:
            output_fn(f"  状态：{operation.get('status')} / {event}")
            last_event = event
        status = operation.get("status")
        if status == "completed":
            output_fn("重置权益已使用并通过双路查询确认。")
            return 0
        if status in {"expired", "disabled"}:
            output_fn(f"任务以终态 {status} 结束。")
            return 2
        sleep_fn(2)


def run_workflow(
    codex_bin: Optional[str] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    install_fn: Callable[[], int] = install_launch_agent,
    wait_fn: Callable[..., int] = wait_for_immediate,
) -> int:
    resolved_codex = prepare(codex_bin, output_fn)
    existing = current_operation()
    if existing and existing.get("status") in ACTIVE_STATUSES:
        output_fn("检测到已有重置任务，保持原权益和幂等键，不重复创建。")
        output_json(manager.public_operation(existing), output_fn)
        install_fn()
        return 0

    output_fn("[2/5] 双路查询并选择权益")
    result = default_cross_validate(codex_bin=resolved_codex)
    credits = available_credits(result.direct_get)
    if not credits:
        output_fn("当前没有可用的 Codex 重置权益。")
        if existing:
            output_json(manager.public_operation(existing), output_fn)
        return 0
    selected = choose_credit(credits, input_fn, output_fn)
    execution_mode = choose_execution_mode(selected, input_fn=input_fn, output_fn=output_fn)

    output_fn("[3/5] 零写入演练")
    dry_operation = validate_dry_run(
        selected,
        result.app_server.available_count,
        execution_mode,
        output_fn=output_fn,
    )
    if not confirm(selected, dry_operation, input_fn, output_fn):
        output_fn("已取消；没有授权、定时或消费权益。")
        return 0

    output_fn("[5/5] 启动执行")
    operation = manager.arm_selected_credit(resolved_codex, selected, execution_mode, authorize_consume=True)
    install_fn()
    output_json(manager.public_operation(operation), output_fn)
    if operation.get("execution_mode") == "immediate":
        try:
            return wait_fn(output_fn=output_fn)
        except KeyboardInterrupt:
            output_fn("前台跟踪已停止；LaunchAgent 仍会在后台继续执行。")
            return 0
    output_fn(f"已定时，将在 {format_beijing_time(operation['trigger_at'])} 自动执行。")
    output_fn("再次运行 make reset 可查看当前任务状态。")
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args:
            raise ResetTransportError("invalid-arguments", "make reset 不需要额外参数")
        return run_workflow()
    except ResetTransportError as exc:
        print(f"reset error [{exc.category}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

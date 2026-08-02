from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import copy_fixture_tree


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "codex_token_usage.py"


def load_module():
    spec = importlib.util.spec_from_file_location("codex_token_usage", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def codex_home(tmp_path):
    return copy_fixture_tree("codex_home", tmp_path)


def freeze_now(module, monkeypatch, fake_now):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_now if tz else fake_now.replace(tzinfo=None)

        @classmethod
        def strptime(cls, date_string, fmt):
            return datetime.strptime(date_string, fmt)

        @classmethod
        def fromtimestamp(cls, ts, tz=None):
            return datetime.fromtimestamp(ts, tz=tz)

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(module, "datetime", FakeDateTime)


def write_session_file(codex_home: Path, day: str, started_at: str, session_id: str, events: list[dict]) -> Path:
    year, month, date = day.split("-")
    target = codex_home / "sessions" / year / month / date / f"rollout-{started_at}-{session_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8")
    return target


def turn_context_event(timestamp: str, model: str, turn_id: str = "turn-default", cwd: str = "/workspace/test") -> dict:
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {
            "turn_id": turn_id,
            "cwd": cwd,
            "model": model,
        },
    }


def thread_settings_applied_event(timestamp: str, service_tier: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "thread_settings_applied",
            "thread_settings": {
                "service_tier": service_tier,
            },
        },
    }


def token_event(
    timestamp: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    last_usage_extra: dict | None = None,
    info_extra: dict | None = None,
) -> dict:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }
    if last_usage_extra:
        usage.update(last_usage_extra)
    total_usage = dict(usage)
    info = {
        "total_token_usage": total_usage,
        "last_token_usage": usage,
        "model_context_window": 258400,
    }
    if info_extra:
        info.update(info_extra)
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": info,
            "rate_limits": None,
        },
    }


def cumulative_token_event(
    timestamp: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    total_input_tokens: int,
    total_cached_input_tokens: int,
    total_output_tokens: int,
    rate_limit_id: str = "",
) -> dict:
    event = token_event(
        timestamp,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        info_extra={
            "total_token_usage": {
                "input_tokens": total_input_tokens,
                "cached_input_tokens": total_cached_input_tokens,
                "output_tokens": total_output_tokens,
                "reasoning_output_tokens": 0,
                "total_tokens": total_input_tokens + total_output_tokens,
            }
        },
    )
    if rate_limit_id:
        event["payload"]["rate_limits"] = {"limit_id": rate_limit_id}
    return event


def test_calculate_cost_supports_gpt_5_6_alias():
    module = load_module()

    cost = module.calculate_cost(
        "gpt-5.6",
        {"input_tokens": 1_000_000, "cached_input_tokens": 200_000, "output_tokens": 300_000},
    )

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(4.0)
    assert cost["hit_cost"] == pytest.approx(0.1)
    assert cost["output_cost"] == pytest.approx(9.0)
    assert cost["total_cost"] == pytest.approx(13.1)


def test_calculate_cost_supports_gpt_5_6_sol():
    module = load_module()

    cost = module.calculate_cost(
        "gpt-5.6-sol",
        {"input_tokens": 2_000_000, "cached_input_tokens": 500_000, "output_tokens": 100_000},
    )

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(7.5)
    assert cost["hit_cost"] == pytest.approx(0.25)
    assert cost["output_cost"] == pytest.approx(3.0)
    assert cost["total_cost"] == pytest.approx(10.75)


def test_calculate_cost_supports_gpt_5_6_terra():
    module = load_module()

    cost = module.calculate_cost(
        "gpt-5.6-terra",
        {"input_tokens": 1_500_000, "cached_input_tokens": 300_000, "output_tokens": 200_000},
    )

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(3.0)
    assert cost["hit_cost"] == pytest.approx(0.075)
    assert cost["output_cost"] == pytest.approx(3.0)
    assert cost["total_cost"] == pytest.approx(6.075)


def test_calculate_cost_supports_gpt_5_6_luna():
    module = load_module()

    cost = module.calculate_cost(
        "gpt-5.6-luna",
        {"input_tokens": 900_000, "cached_input_tokens": 400_000, "output_tokens": 250_000},
    )

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(0.5)
    assert cost["hit_cost"] == pytest.approx(0.04)
    assert cost["output_cost"] == pytest.approx(1.5)
    assert cost["total_cost"] == pytest.approx(2.04)


def test_collect_usage_report_prices_gpt_5_6_alias_model(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "34343434-3434-4434-8434-343434343434"
    write_session_file(
        codex_home,
        "2026-07-25",
        "2026-07-25T09-00-00",
        session_id,
        [
            turn_context_event(
                "2026-07-25T01:00:00.000Z",
                "gpt-5.6",
                turn_id="turn-gpt-5.6-alias",
                cwd="/workspace/gpt-5-6-alias",
            ),
            token_event(
                "2026-07-25T01:00:05.000Z",
                1_000_000,
                250_000,
                400_000,
                last_usage_extra={"cache_write_input_tokens": 0},
            ),
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 25, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 26, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    total_cost, cost_status = module.calculate_models_cost(report["models"])

    assert report["models"]["gpt-5.6"]["input_tokens"] == 1_000_000
    assert report["models"]["gpt-5.6"]["cached_input_tokens"] == 250_000
    assert report["models"]["gpt-5.6"]["output_tokens"] == 400_000
    assert total_cost == pytest.approx(25.75)
    assert cost_status == {"unknown_model": False, "unrecoverable_cache_write": False}


def test_calculate_event_cost_gpt_5_6_sol_default_short_context():
    module = load_module()
    usage = {
        "input_tokens": 200_000,
        "cached_input_tokens": 50_000,
        "output_tokens": 10_000,
        "cache_write_input_tokens": 20_000,
    }

    cost = module.calculate_event_cost("gpt-5.6-sol", usage, "default", {"last_token_usage": usage})

    assert cost is not None
    assert cost["cache_write_tokens"] == 20_000
    assert cost["miss_cost"] == pytest.approx(0.65)
    assert cost["hit_cost"] == pytest.approx(0.025)
    assert cost["write_cost"] == pytest.approx(0.125)
    assert cost["output_cost"] == pytest.approx(0.3)
    assert cost["total_cost"] == pytest.approx(1.1)
    assert cost["partial_unrecoverable_cache_write"] is False


def test_calculate_event_cost_gpt_5_6_alias_matches_sol_rates():
    module = load_module()
    usage = {
        "input_tokens": 200_000,
        "cached_input_tokens": 50_000,
        "output_tokens": 10_000,
        "cache_write_input_tokens": 20_000,
    }

    alias_cost = module.calculate_event_cost("gpt-5.6", usage, "default", {"last_token_usage": usage})
    sol_cost = module.calculate_event_cost("gpt-5.6-sol", usage, "default", {"last_token_usage": usage})

    assert alias_cost == sol_cost


def test_calculate_event_cost_gpt_5_6_terra_default_short_context():
    module = load_module()
    usage = {
        "input_tokens": 200_000,
        "cached_input_tokens": 50_000,
        "output_tokens": 20_000,
        "cache_write_input_tokens": 10_000,
    }

    cost = module.calculate_event_cost("gpt-5.6-terra", usage, "default", {"last_token_usage": usage})

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(0.35)
    assert cost["hit_cost"] == pytest.approx(0.0125)
    assert cost["write_cost"] == pytest.approx(0.03125)
    assert cost["output_cost"] == pytest.approx(0.3)
    assert cost["total_cost"] == pytest.approx(0.69375)
    assert cost["partial_unrecoverable_cache_write"] is False


def test_calculate_event_cost_gpt_5_6_luna_default_short_context():
    module = load_module()
    usage = {
        "input_tokens": 100_000,
        "cached_input_tokens": 10_000,
        "output_tokens": 15_000,
        "cache_write_input_tokens": 5_000,
    }

    cost = module.calculate_event_cost("gpt-5.6-luna", usage, "default", {"last_token_usage": usage})

    assert cost is not None
    assert cost["miss_cost"] == pytest.approx(0.085)
    assert cost["hit_cost"] == pytest.approx(0.001)
    assert cost["write_cost"] == pytest.approx(0.00625)
    assert cost["output_cost"] == pytest.approx(0.09)
    assert cost["total_cost"] == pytest.approx(0.18225)
    assert cost["partial_unrecoverable_cache_write"] is False


def test_collect_usage_report_prices_mixed_gpt_5_6_sol_short_and_long_events_per_event(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "56565656-5656-4565-8565-565656565656"
    write_session_file(
        codex_home,
        "2026-07-25",
        "2026-07-25T10-30-00",
        session_id,
        [
            turn_context_event(
                "2026-07-25T02:30:00.000Z",
                "gpt-5.6-sol",
                turn_id="turn-gpt-5.6-sol-mixed",
                cwd="/workspace/gpt-5-6-sol-mixed",
            ),
            token_event(
                "2026-07-25T02:30:05.000Z",
                200_000,
                100_000,
                10_000,
                last_usage_extra={"cache_write_input_tokens": 0},
            ),
            token_event(
                "2026-07-25T02:30:15.000Z",
                300_000,
                50_000,
                20_000,
                last_usage_extra={"cache_write_input_tokens": 0},
            ),
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 25, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 26, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    total_cost, cost_status = module.calculate_models_cost(report["models"])

    assert report["models"]["gpt-5.6-sol"]["input_tokens"] == 500_000
    assert report["models"]["gpt-5.6-sol"]["cached_input_tokens"] == 150_000
    assert report["models"]["gpt-5.6-sol"]["output_tokens"] == 30_000
    assert total_cost == pytest.approx(4.3)
    assert cost_status == {"unknown_model": False, "unrecoverable_cache_write": False}


def test_collect_usage_report_uses_priority_service_tier_for_gpt_5_6_sol(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "78787878-7878-4787-8787-787878787878"
    write_session_file(
        codex_home,
        "2026-07-25",
        "2026-07-25T11-00-00",
        session_id,
        [
            turn_context_event(
                "2026-07-25T03:00:00.000Z",
                "gpt-5.6-sol",
                turn_id="turn-gpt-5.6-sol-priority",
                cwd="/workspace/gpt-5-6-sol-priority",
            ),
            thread_settings_applied_event("2026-07-25T03:00:01.000Z", "priority"),
            token_event(
                "2026-07-25T03:00:05.000Z",
                100_000,
                20_000,
                10_000,
                last_usage_extra={"cache_write_input_tokens": 0},
            ),
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 25, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 26, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    total_cost, cost_status = module.calculate_models_cost(report["models"])

    assert total_cost == pytest.approx(1.42)
    assert cost_status == {"unknown_model": False, "unrecoverable_cache_write": False}


def test_build_summary_lines_marks_unrecoverable_cache_write_for_gpt_5_6_when_field_is_absent(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "90909090-9090-4090-8090-909090909090"
    write_session_file(
        codex_home,
        "2026-07-25",
        "2026-07-25T12-00-00",
        session_id,
        [
            turn_context_event(
                "2026-07-25T04:00:00.000Z",
                "gpt-5.6-sol",
                turn_id="turn-gpt-5.6-sol-unrecoverable-cache-write",
                cwd="/workspace/gpt-5-6-sol-unrecoverable-cache-write",
            ),
            token_event("2026-07-25T04:00:05.000Z", 200_000, 50_000, 10_000),
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 25, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 26, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    lines = module.build_summary_lines(report, start_local, end_local)

    assert "估算总成本：$1.07（未含无法从日志恢复的 cache write）" in lines

def test_collect_usage_handles_null_info_and_non_string_function_output(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 27, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    results = module.collect_usage(start_local, end_local)

    assert results["gpt-5.4-mini"]["input_tokens"] == 650
    assert results["gpt-5.4-mini"]["cached_input_tokens"] == 210
    assert results["gpt-5.4-mini"]["output_tokens"] == 67
    assert results["gpt-5.4"]["input_tokens"] == 400
    assert results["gpt-5.4"]["cached_input_tokens"] == 100
    assert results["gpt-5.4"]["output_tokens"] == 40


def test_collect_usage_aggregates_normal_records(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 25, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 26, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    results = module.collect_usage(start_local, end_local)

    assert results["gpt-5.4-mini"]["input_tokens"] == 12
    assert results["gpt-5.4-mini"]["cached_input_tokens"] == 4
    assert results["gpt-5.4-mini"]["output_tokens"] == 3


def test_collect_usage_aggregates_multiple_entries_in_one_session(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 4, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 5, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    results = module.collect_usage(start_local, end_local)

    assert results["gpt-5"]["input_tokens"] == 600_000_000
    assert results["gpt-5"]["cached_input_tokens"] == 500_000_000
    assert results["gpt-5"]["output_tokens"] == 200_000_000


def test_collect_usage_report_ignores_model_names_inside_function_call_output(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "12121212-1212-4212-8212-121212121212"
    write_session_file(
        codex_home,
        "2026-07-22",
        "2026-07-22T14-10-32",
        session_id,
        [
            {
                "timestamp": "2026-07-22T06:10:32.172Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "cwd": "/workspace/model-attribution",
                },
            },
            {
                "timestamp": "2026-07-22T06:10:36.253Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-model-attribution",
                    "cwd": "/workspace/model-attribution",
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-07-22T09:42:25.734Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-foreign-model-json",
                    "output": (
                        '{"assistantMessage":{"extra":{"model":"claude-sonnet-4-6",'
                        '"provider":"codeagent","usage":{"total_tokens":4340}}}}'
                    ),
                },
            },
            token_event("2026-07-22T09:42:25.735Z", 171_739, 153_984, 740),
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 22, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 23, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    session = report["sessions"][session_id]

    assert report["models"]["gpt-5.4"]["input_tokens"] == 171_739
    assert report["models"]["gpt-5.4"]["cached_input_tokens"] == 153_984
    assert report["models"]["gpt-5.4"]["output_tokens"] == 740
    assert "claude-sonnet-4-6" not in report["models"]
    assert session["models"]["gpt-5.4"]["input_tokens"] == 171_739
    assert session["models"]["gpt-5.4"]["cached_input_tokens"] == 153_984
    assert session["models"]["gpt-5.4"]["output_tokens"] == 740
    assert "claude-sonnet-4-6" not in session["models"]


def test_format_token_count_uses_expected_units():
    module = load_module()

    assert module.format_token_count(3_000_000) == "3百万（3,000,000）"
    assert module.format_token_count(300_000) == "0.3百万（300,000）"
    assert module.format_token_count(50_000_000) == "5千万（50,000,000）"
    assert module.format_token_count(800_000_000) == "8亿（800,000,000）"
    assert module.format_token_count(1_250_000) == "1.25百万（1,250,000）"


def test_build_activity_spans_uses_turn_boundaries():
    module = load_module()
    tz = module.ZoneInfo("Asia/Shanghai")
    turns = {
        "turn-a": {
            "started_at": datetime(2026, 7, 1, 10, 0, 0, tzinfo=tz),
            "last_progress_at": datetime(2026, 7, 1, 10, 12, 0, tzinfo=tz),
            "completed_at": None,
        },
        "turn-b": {
            "started_at": datetime(2026, 7, 1, 10, 40, 0, tzinfo=tz),
            "last_progress_at": datetime(2026, 7, 1, 10, 43, 0, tzinfo=tz),
            "completed_at": datetime(2026, 7, 1, 10, 45, 0, tzinfo=tz),
        },
        "turn-c": {
            "started_at": datetime(2026, 7, 1, 11, 0, 0, tzinfo=tz),
            "last_progress_at": None,
            "completed_at": None,
        },
    }

    spans = module.build_activity_spans(turns)

    assert spans == [
        (datetime(2026, 7, 1, 10, 0, 0, tzinfo=tz), datetime(2026, 7, 1, 10, 12, 0, tzinfo=tz)),
        (datetime(2026, 7, 1, 10, 40, 0, tzinfo=tz), datetime(2026, 7, 1, 10, 45, 0, tzinfo=tz)),
    ]


def test_sum_activity_seconds_clamps_window():
    module = load_module()
    tz = module.ZoneInfo("Asia/Shanghai")
    spans = [
        (datetime(2026, 7, 1, 10, 0, 0, tzinfo=tz), datetime(2026, 7, 1, 10, 12, 0, tzinfo=tz)),
        (datetime(2026, 7, 1, 10, 40, 0, tzinfo=tz), datetime(2026, 7, 1, 10, 45, 0, tzinfo=tz)),
    ]

    total_seconds = module.sum_activity_seconds(
        spans,
        datetime(2026, 7, 1, 10, 5, 0, tzinfo=tz),
        datetime(2026, 7, 1, 10, 45, 0, tzinfo=tz),
    )

    assert total_seconds == 720


def test_format_duration_uses_expected_text():
    module = load_module()

    assert module.format_duration(0) == "0分钟"
    assert module.format_duration(300) == "5分钟"
    assert module.format_duration(3_600) == "1小时"
    assert module.format_duration(3_900) == "1小时5分钟"
    assert module.format_duration(898) == "15分钟"


def test_extract_semantic_title_text_skips_noise_and_merges_lines():
    module = load_module()

    text = """
    # AGENTS.md instructions for /workspace/demo

    <INSTRUCTIONS>
    Always respond in 简体中文
    </INSTRUCTIONS>
    [MODE: EXECUTE]
    % make today
    真正的第一句标题
    第二行补充
    """

    assert module.extract_semantic_title_text(text) == "真正的第一句标题 第二行补充"
    assert module.extract_semantic_title_text('{"status":"ok"}') is None


def test_extract_semantic_title_text_ignores_recommended_plugins_block():
    module = load_module()

    text = """
    <recommended_plugins>
    Here is a list of plugins that are available but not installed.

    - Airtable (airtable@openai-curated-remote)
    - Asana (asana@openai-curated-remote)
    </recommended_plugins>
    """

    assert module.extract_semantic_title_text(text) is None


def test_extract_semantic_title_text_keeps_real_text_after_recommended_plugins():
    module = load_module()

    text = """
    <RECOMMENDED_PLUGINS source="remote">
    Here is a list of plugins that are available but not installed.
    - Airtable (airtable@openai-curated-remote)
    </RECOMMENDED_PLUGINS>
    继续作为任务管理员 方方 推进以下任务。
    """

    assert module.extract_semantic_title_text(text) == "继续作为任务管理员 方方 推进以下任务。"


def test_build_session_title_joins_next_message_when_first_is_short():
    module = load_module()

    assert module.build_session_title("第一句。第二句", "fallback") == "第一句。第二句"
    assert module.build_session_title("\n\n第一行   标题\n第二行", "fallback") == "第一行 标题 第二行"
    assert module.build_session_title(["第一条", "第二条补充"], "fallback") == "第一条 | 第二条补充"
    assert module.build_session_title("甲" * 240, "fallback") == "甲" * 200


def test_collect_usage_report_uses_real_prompt_after_recommended_plugins(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "77777777-7777-4777-8777-777777777777"
    write_session_file(
        codex_home,
        "2026-07-05",
        "2026-07-05T10-00-00",
        session_id,
        [
            {
                "timestamp": "2026-07-05T02:00:00.000Z",
                "type": "session_meta",
                "payload": {"session_id": session_id, "id": session_id, "cwd": "/workspace/plugins"},
            },
            {
                "timestamp": "2026-07-05T02:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "<recommended_plugins>\n"
                                "Here is a list of plugins that are available but not installed.\n"
                                "- Airtable (airtable@openai-curated-remote)\n"
                                "</recommended_plugins>"
                            ),
                        },
                        {
                            "type": "input_text",
                            "text": "# AGENTS.md instructions\n<INSTRUCTIONS>Always respond in 简体中文</INSTRUCTIONS>",
                        },
                    ],
                },
            },
            {
                "timestamp": "2026-07-05T02:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "继续作为任务管理员 方方 推进以下任务。"}],
                },
            },
            turn_context_event("2026-07-05T02:00:03.000Z", "gpt-5.6-sol", cwd="/workspace/plugins"),
            cumulative_token_event("2026-07-05T02:01:00.000Z", 100, 40, 10, 100, 40, 10),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 5, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)

    assert report["sessions"][session_id]["title"] == "继续作为任务管理员 方方 推进以下任务。"


def test_collect_usage_report_deduplicates_repeated_total_usage_snapshots(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "88888888-8888-4888-8888-888888888888"
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T10-00-00",
        session_id,
        [
            turn_context_event("2026-07-06T02:00:00.000Z", "gpt-5.6-sol"),
            cumulative_token_event("2026-07-06T02:01:00.000Z", 100, 40, 10, 100, 40, 10, "codex"),
            cumulative_token_event("2026-07-06T02:02:00.000Z", 100, 40, 10, 100, 40, 10, "codex_bengalfox"),
            cumulative_token_event("2026-07-06T02:03:00.000Z", 100, 40, 10, 100, 40, 10, "codex"),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)
    session = report["sessions"][session_id]

    assert session["input_tokens"] == 100
    assert session["cached_input_tokens"] == 40
    assert session["output_tokens"] == 10


def test_collect_usage_report_deduplicates_snapshot_after_intervening_activity(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "99999999-9999-4999-8999-999999999999"
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T11-00-00",
        session_id,
        [
            turn_context_event("2026-07-06T03:00:00.000Z", "gpt-5.6-sol"),
            cumulative_token_event("2026-07-06T03:01:00.000Z", 100, 40, 10, 100, 40, 10),
            {
                "timestamp": "2026-07-06T03:02:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "处理中"}],
                },
            },
            cumulative_token_event("2026-07-06T03:03:00.000Z", 100, 40, 10, 100, 40, 10),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    session = module.collect_usage_report(start_local, end_local)["sessions"][session_id]

    assert session["input_tokens"] == 100
    assert session["output_tokens"] == 10


def test_collect_usage_report_counts_matching_last_usage_when_cumulative_total_grows(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T12-00-00",
        session_id,
        [
            turn_context_event("2026-07-06T04:00:00.000Z", "gpt-5.6-sol"),
            cumulative_token_event("2026-07-06T04:01:00.000Z", 100, 40, 10, 100, 40, 10),
            cumulative_token_event("2026-07-06T04:02:00.000Z", 100, 40, 10, 200, 80, 20),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    session = module.collect_usage_report(start_local, end_local)["sessions"][session_id]

    assert session["input_tokens"] == 200
    assert session["cached_input_tokens"] == 80
    assert session["output_tokens"] == 20


def test_collect_usage_report_deduplicates_snapshot_replayed_across_date_boundary(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
    write_session_file(
        codex_home,
        "2026-07-05",
        "2026-07-05T23-59-00",
        session_id,
        [
            turn_context_event("2026-07-05T15:59:00.000Z", "gpt-5.6-sol"),
            cumulative_token_event("2026-07-05T15:59:50.000Z", 100, 40, 10, 100, 40, 10),
            cumulative_token_event("2026-07-05T16:00:10.000Z", 100, 40, 10, 100, 40, 10),
            cumulative_token_event("2026-07-05T16:01:00.000Z", 50, 20, 5, 150, 60, 15),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    session = module.collect_usage_report(start_local, end_local)["sessions"][session_id]

    assert session["input_tokens"] == 50
    assert session["cached_input_tokens"] == 20
    assert session["output_tokens"] == 5


def test_collect_usage_report_keeps_legacy_events_without_total_usage(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
    first = token_event("2026-07-06T05:01:00.000Z", 100, 40, 10, info_extra={"total_token_usage": None})
    second = token_event("2026-07-06T05:02:00.000Z", 100, 40, 10, info_extra={"total_token_usage": None})
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T13-00-00",
        session_id,
        [turn_context_event("2026-07-06T05:00:00.000Z", "gpt-5.6-sol"), first, second],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    session = module.collect_usage_report(start_local, end_local)["sessions"][session_id]

    assert session["input_tokens"] == 200
    assert session["cached_input_tokens"] == 80
    assert session["output_tokens"] == 20


def test_collect_usage_report_counts_cumulative_counter_reset(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "dddddddd-dddd-4ddd-8ddd-ddddddddddd3"
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T14-00-00",
        session_id,
        [
            turn_context_event("2026-07-06T06:00:00.000Z", "gpt-5.6-sol"),
            cumulative_token_event("2026-07-06T06:01:00.000Z", 200, 80, 20, 200, 80, 20),
            cumulative_token_event("2026-07-06T06:02:00.000Z", 50, 20, 5, 50, 20, 5),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    session = module.collect_usage_report(start_local, end_local)["sessions"][session_id]

    assert session["input_tokens"] == 250
    assert session["cached_input_tokens"] == 100
    assert session["output_tokens"] == 25


def test_collect_usage_report_duplicate_snapshot_does_not_extend_activity(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee4"
    write_session_file(
        codex_home,
        "2026-07-06",
        "2026-07-06T15-00-00",
        session_id,
        [
            turn_context_event("2026-07-06T07:00:00.000Z", "gpt-5.6-sol", turn_id="turn-activity"),
            cumulative_token_event("2026-07-06T07:01:00.000Z", 100, 40, 10, 100, 40, 10),
            cumulative_token_event("2026-07-06T07:30:00.000Z", 100, 40, 10, 100, 40, 10),
        ],
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 6, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 7, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)

    assert report["sessions"][session_id]["active_seconds"] == 60
    assert report["active_seconds"] == 60


def test_collect_usage_report_includes_session_title_and_totals(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 3, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 4, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    session = report["sessions"]["55555555-5555-4555-8555-555555555555"]

    assert session["title"] == "2026年7月3日这是第一行标题，会被截取并保留到句号。第二句不应进入标题。 第二行不应进入标题"
    assert session["cwd"] == "/workspace/usage-july-3"
    assert session["input_tokens"] == 50_000_000
    assert session["cached_input_tokens"] == 15_000_000
    assert session["output_tokens"] == 3_000_000
    assert session["active_seconds"] == 4_199
    assert report["active_seconds"] == 4_199


def test_collect_usage_report_uses_semantic_titles_for_june_27(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 27, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    sessions = report["sessions"]

    assert sessions["019f06bb-e689-7ab0-8348-6981560cc7af"]["title"].startswith(
        "使用当前软件工厂提供的增长获客SEO PDCA循环，对以下2个项目启动增长获客迭代："
    )
    assert "/Users/pocket/Documents/project/24h-studio" in sessions["019f06bb-e689-7ab0-8348-6981560cc7af"]["title"]
    assert sessions["019f075e-3dfc-7d42-b7e9-e9ceec7f97a6"]["title"] == "帮我统计今天的 token 使用并解释异常原因。第二句不应进入标题。"
    assert sessions["019f098e-5c62-7622-8fba-d1c445b9010f"]["title"] == "你是软件工厂里的增长研究负责人。请分析最近30天的增长异常。"
    assert sessions["019f096a-19fc-7091-8e94-19f1c38a2501"]["title"] == "019f096a-19fc-7091-8e94-19f1c38a2501"
    assert all(session["title"] != "[MODE: EXECUTE]" for session in sessions.values())
    assert all(session["title"] != '{"status":"ok"}' for session in sessions.values())
    assert all(session["title"] != "% make today" for session in sessions.values())


def test_collect_usage_report_keeps_june_27_sessions_separate(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 27, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)

    assert set(report["sessions"]) == {
        "33333333-3333-4333-8333-333333333333",
        "019f06bb-e689-7ab0-8348-6981560cc7af",
        "019f075e-3dfc-7d42-b7e9-e9ceec7f97a6",
        "019f098e-5c62-7622-8fba-d1c445b9010f",
        "019f096a-19fc-7091-8e94-19f1c38a2501",
    }


def test_collect_usage_report_uses_semantic_titles_for_june_28(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 29, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)

    assert report["sessions"]["019f09d2-c8b8-7a20-af8a-75ba2ed310dd"]["title"] == "你是软件工厂里的增长研究负责人。请调研 AI SEO 选题。"
    assert report["sessions"]["019f0ae1-adae-7370-a503-81392118d281"]["title"] == "请分析6月28日 session 标题异常并输出结论。第二句不要进入标题。"


def test_collect_usage_report_aggregates_child_sessions_under_parent(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    project_root = tmp_path / "child-project"
    project_root.mkdir(parents=True)
    studio_root = tmp_path / "24h-studio"
    studio_root.mkdir(parents=True)
    run_dir = project_root / ".24h-studio" / "runs" / "20260628-045603-growth_market_research"
    run_dir.mkdir(parents=True)
    (run_dir / "stderr.log").write_text("session id: bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1\n", encoding="utf-8")
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "kind": "growth_market_research",
                "started_at": "2026-06-27T20:56:03+00:00",
                "finished_at": "2026-06-27T20:59:33+00:00",
            }
        ),
        encoding="utf-8",
    )

    parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
    child_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
    write_session_file(
        codex_home,
        "2026-06-27",
        "2026-06-27T20-54-00",
        parent_id,
        [
            {
                "timestamp": "2026-06-27T20:54:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-parent",
                    "started_at": 1782593640,
                },
            },
            {
                "timestamp": "2026-06-27T20:54:00.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": parent_id,
                    "id": parent_id,
                    "cwd": str(studio_root),
                },
            },
            {
                "timestamp": "2026-06-27T20:54:01.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-parent",
                    "cwd": str(studio_root),
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-06-27T20:54:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": f"使用当前软件工厂启动增长循环：\n{project_root}",
                    "images": [],
                },
            },
            {
                "timestamp": "2026-06-27T20:55:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {"cmd": f"python -m studio growth --project {project_root} --deployment-url https://example.com"},
                        ensure_ascii=False,
                    ),
                },
            },
            {
                "timestamp": "2026-06-27T20:55:10.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-parent",
                },
            },
        ],
    )
    write_session_file(
        codex_home,
        "2026-06-28",
        "2026-06-28T04-56-03",
        child_id,
        [
            {
                "timestamp": "2026-06-27T20:56:03.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-child",
                    "started_at": 1782593763,
                },
            },
            {
                "timestamp": "2026-06-27T20:56:03.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": child_id,
                    "id": child_id,
                    "cwd": str(project_root),
                },
            },
            {
                "timestamp": "2026-06-27T20:56:04.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-child",
                    "cwd": str(project_root),
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-06-27T20:56:05.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "你是软件工厂里的增长研究负责人。请分析这个项目的增长机会。",
                    "images": [],
                },
            },
            token_event("2026-06-27T20:57:00.000Z", 100, 40, 10),
            {
                "timestamp": "2026-06-27T20:57:10.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-child",
                },
            },
        ],
    )

    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 29, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)

    assert set(report["sessions"]) == {parent_id, child_id}
    assert report["sessions"][parent_id]["active_seconds"] == 70
    assert len(report["session_display"]) == 1
    display = report["session_display"][0]
    assert display["session_id"] == parent_id
    assert display["display_kind"] == "group"
    assert display["child_count"] == 1
    assert display["project_roots"] == [str(project_root)]
    assert display["input_tokens"] == 100
    assert display["cached_input_tokens"] == 40
    assert display["output_tokens"] == 10
    assert display["active_seconds"] == 137
    assert report["session_clusters"] == []


def test_collect_usage_report_clusters_unresolved_child_sessions_by_cwd(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    project_root = tmp_path / "cluster-project"
    project_root.mkdir(parents=True)

    child_a = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
    child_b = "dddddddd-dddd-4ddd-8ddd-ddddddddddd2"

    for suffix, session_id, started_at, event_ts, token_ts, complete_ts, file_started_at, token_input, token_output in [
        (
            "20260628-045603-growth_market_research",
            child_a,
            "2026-06-27T20:56:03+00:00",
            "2026-06-27T20:56:03.000Z",
            "2026-06-27T20:56:48.000Z",
            "2026-06-27T20:56:58.000Z",
            "2026-06-28T04-56-03",
            100,
            10,
        ),
        (
            "20260628-050000-growth_seo_content_task",
            child_b,
            "2026-06-27T21:00:00+00:00",
            "2026-06-27T21:00:00.000Z",
            "2026-06-27T21:01:10.000Z",
            "2026-06-27T21:01:30.000Z",
            "2026-06-28T05-00-00",
            200,
            20,
        ),
    ]:
        run_dir = project_root / ".24h-studio" / "runs" / suffix
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "stderr.log").write_text(f"session id: {session_id}\n", encoding="utf-8")
        (run_dir / "meta.json").write_text(
            json.dumps({"kind": "growth_task", "started_at": started_at, "finished_at": started_at}),
            encoding="utf-8",
        )
        write_session_file(
            codex_home,
            "2026-06-28",
            file_started_at,
            session_id,
            [
                {
                    "timestamp": event_ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": f"turn-{session_id}",
                        "started_at": 1782593763,
                    },
                },
                {
                    "timestamp": event_ts,
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "id": session_id,
                        "cwd": str(project_root),
                    },
                },
                {
                    "timestamp": event_ts,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": f"turn-{session_id}",
                        "cwd": str(project_root),
                        "model": "gpt-5.4",
                    },
                },
                {
                    "timestamp": event_ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "你是软件工厂里的 SEO 内容创作负责人。请处理一个新任务。",
                        "images": [],
                    },
                },
                token_event(token_ts, token_input, token_input // 2, token_output),
                {
                    "timestamp": complete_ts,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{session_id}",
                    },
                },
            ],
        )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 29, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)

    assert report["session_display"] == []
    assert len(report["session_clusters"]) == 1
    cluster = report["session_clusters"][0]
    assert cluster["cwd"] == str(project_root)
    assert cluster["session_count"] == 2
    assert cluster["usage"]["input_tokens"] == 300
    assert cluster["usage"]["output_tokens"] == 30
    assert cluster["average_tokens"] == 165
    assert cluster["median_tokens"] == 165
    assert cluster["p90_tokens"] == 220
    assert cluster["max_tokens"] == 220
    assert cluster["active_seconds"] == 145
    assert cluster["average_active_seconds"] == 72
    assert cluster["median_active_seconds"] == 72
    assert cluster["p90_active_seconds"] == 90
    assert cluster["max_active_seconds"] == 90


def test_collect_usage_report_includes_activity_only_session(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
    write_session_file(
        codex_home,
        "2026-07-04",
        "2026-07-04T10-00-00",
        session_id,
        [
            {
                "timestamp": "2026-07-04T02:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "cwd": "/workspace/activity-only",
                },
            },
            {
                "timestamp": "2026-07-04T02:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-activity-only",
                    "started_at": 1783130400,
                },
            },
            {
                "timestamp": "2026-07-04T02:00:01.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-activity-only",
                    "cwd": "/workspace/activity-only",
                    "model": "gpt-5.4-mini",
                },
            },
            {
                "timestamp": "2026-07-04T02:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "这是一个只有活跃时长没有 token 的会话。",
                    "images": [],
                },
            },
            {
                "timestamp": "2026-07-04T02:00:05.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "Codex 已开始处理该请求。",
                },
            },
            {
                "timestamp": "2026-07-04T02:00:08.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-activity-only",
                },
            },
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 4, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 5, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    report = module.collect_usage_report(start_local, end_local)

    assert report["active_seconds"] == 8
    assert set(report["sessions"]) == {session_id}
    session = report["sessions"][session_id]
    assert session["input_tokens"] == 0
    assert session["output_tokens"] == 0
    assert session["active_seconds"] == 8
    assert len(report["session_display"]) == 1
    assert report["session_display"][0]["title"] == "这是一个只有活跃时长没有 token 的会话。"
    assert report["session_display"][0]["active_seconds"] == 8


def test_collect_recent_usage_merges_overlapping_daily_activity(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"

    for index, minute_offset in enumerate([0, 3], start=1):
        session_id = f"ffffffff-ffff-4fff-8fff-fffffffffff{index}"
        write_session_file(
            codex_home,
            "2026-07-04",
            f"2026-07-04T10-0{minute_offset}-00",
            session_id,
            [
                {
                    "timestamp": f"2026-07-04T02:0{minute_offset}:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": f"turn-recent-overlap-{index}",
                        "started_at": 1783130400 + minute_offset * 60,
                    },
                },
                {
                    "timestamp": f"2026-07-04T02:0{minute_offset}:00.000Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "id": session_id,
                        "cwd": f"/workspace/recent-overlap-{index}",
                    },
                },
                {
                    "timestamp": f"2026-07-04T02:0{minute_offset}:01.000Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": f"turn-recent-overlap-{index}",
                        "cwd": f"/workspace/recent-overlap-{index}",
                        "model": "gpt-5.4-mini",
                    },
                },
                {
                    "timestamp": f"2026-07-04T02:0{minute_offset}:02.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": f"重叠活跃测试 {index}",
                        "images": [],
                    },
                },
                token_event(f"2026-07-04T02:0{minute_offset + 5}:00.000Z", 100, 20, 10),
                {
                    "timestamp": f"2026-07-04T02:0{minute_offset + 6}:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-recent-overlap-{index}",
                    },
                },
            ],
        )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 4, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 5, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    recent = module.collect_recent_usage(start_local, end_local, 1)

    assert recent["active_seconds"] == 540
    assert recent["days"][0]["active_seconds"] == 540


def test_collect_recent_usage_splits_cross_day_activity(monkeypatch, tmp_path):
    module = load_module()
    codex_home = tmp_path / "codex_home"
    session_id = "abababab-abab-4aba-8aba-abababababa1"
    write_session_file(
        codex_home,
        "2026-07-03",
        "2026-07-03T23-58-00",
        session_id,
        [
            {
                "timestamp": "2026-07-03T15:58:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "turn-recent-cross-day",
                    "started_at": 1783094280,
                },
            },
            {
                "timestamp": "2026-07-03T15:58:00.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": session_id,
                    "id": session_id,
                    "cwd": "/workspace/recent-cross-day",
                },
            },
            {
                "timestamp": "2026-07-03T15:58:01.000Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "turn-recent-cross-day",
                    "cwd": "/workspace/recent-cross-day",
                    "model": "gpt-5.4-mini",
                },
            },
            {
                "timestamp": "2026-07-03T15:58:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "跨天活跃时长测试",
                    "images": [],
                },
            },
            token_event("2026-07-03T16:10:00.000Z", 100, 20, 10),
            {
                "timestamp": "2026-07-03T16:12:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-recent-cross-day",
                },
            },
        ],
    )

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    start_local = datetime(2026, 7, 3, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 5, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    recent = module.collect_recent_usage(start_local, end_local, 2)

    assert [record["active_seconds"] for record in recent["days"]] == [120, 720]


def test_main_date_summary_only_writes_full_detail(monkeypatch, codex_home, tmp_path, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    detail_file = tmp_path / "detail-date.txt"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_token_usage.py",
            "-d",
            "2026-07-03",
            "-z",
            "Asia/Shanghai",
            "-s",
            "-f",
            str(detail_file),
        ],
    )

    assert module.main() == 0
    output = capsys.readouterr().out
    detail_text = detail_file.read_text(encoding="utf-8")

    assert "日期：2026-07-03" in output
    assert "总Token：5.3千万（53,000,000）" in output
    assert "活跃时长：1小时10分钟" in output
    assert "三、Session 明细" not in output
    assert "三、Session 明细" in detail_text
    assert "3.1 2026年7月3日这是第一行标题，会被截取并保留到句号。第二句不应进入标题。 第二行不应进入标题" in detail_text
    assert "活跃时长：1小时10分钟" in detail_text


def test_main_today_summary_only_succeeds(monkeypatch, codex_home, tmp_path, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    detail_file = tmp_path / "detail-today.txt"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_token_usage.py",
            "-t",
            "-z",
            "Asia/Shanghai",
            "-s",
            "-f",
            str(detail_file),
        ],
    )
    fake_now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    freeze_now(module, monkeypatch, fake_now)

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "日期：2026-07-04" in output
    assert "总Token：8亿（800,000,000）" in output
    assert "活跃时长：2小时10分钟" in output
    assert detail_file.exists()


def test_main_recent_30_days_writes_markdown_detail(monkeypatch, codex_home, tmp_path, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    detail_file = tmp_path / "recent-detail.md"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_token_usage.py",
            "-r",
            "-n",
            "30",
            "-f",
            str(detail_file),
        ],
    )
    fake_now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    freeze_now(module, monkeypatch, fake_now)

    assert module.main() == 0
    output = capsys.readouterr().out
    detail_text = detail_file.read_text(encoding="utf-8")

    assert "一、统计" in output
    assert "天数：30" in output
    assert "活跃天数：5" in output
    assert "总Token：8.54亿（854,302,465）" in output
    assert "总成本：$2,324.90（部分模型未计价）" in output
    assert "总活跃时长：3小时32分钟" in output
    assert "2026-07-04 | 总Token 8亿（800,000,000） | 成本 $2,187.50 | 活跃时长 2小时10分钟" in output
    assert "# 最近 Token、成本与活跃时长明细" in detail_text
    assert "| 2026-07-04 | 8亿（800,000,000） | $2,187.50 | 2小时10分钟 |" in detail_text


def test_main_recent_one_day_only_counts_today(monkeypatch, codex_home, tmp_path, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    detail_file = tmp_path / "recent-one-day.md"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_token_usage.py",
            "-r",
            "-n",
            "1",
            "-s",
            "-f",
            str(detail_file),
        ],
    )
    fake_now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    freeze_now(module, monkeypatch, fake_now)

    assert module.main() == 0
    output = capsys.readouterr().out
    detail_text = detail_file.read_text(encoding="utf-8")

    assert "天数：1" in output
    assert "活跃天数：1" in output
    assert "总Token：8亿（800,000,000）" in output
    assert "总成本：$2,187.50" in output
    assert "总活跃时长：2小时10分钟" in output
    assert "| 2026-07-04 | 8亿（800,000,000） | $2,187.50 | 2小时10分钟 |" in detail_text


def test_main_missing_sessions_returns_error(monkeypatch, tmp_path, capsys):
    module = load_module()
    missing_home = tmp_path / "missing_home"
    missing_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(missing_home))
    monkeypatch.setattr(module.sys, "argv", ["codex_token_usage.py", "-t"])

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "session directory not found" in err


def test_main_rejects_mixed_time_modes(monkeypatch, codex_home, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(module.sys, "argv", ["codex_token_usage.py", "-t", "-d", "2026-07-03"])

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "只能选择一种时间范围输入方式：-t、-d、-r 或 start_time/end_time" in err


def test_main_rejects_invalid_recent_days(monkeypatch, codex_home, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(module.sys, "argv", ["codex_token_usage.py", "-r", "-n", "0"])

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "-n 必须是大于 0 的整数" in err

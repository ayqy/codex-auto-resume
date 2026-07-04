from __future__ import annotations

import importlib.util
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


def test_format_token_count_uses_expected_units():
    module = load_module()

    assert module.format_token_count(3_000_000) == "3百万（3,000,000）"
    assert module.format_token_count(300_000) == "0.3百万（300,000）"
    assert module.format_token_count(50_000_000) == "5千万（50,000,000）"
    assert module.format_token_count(800_000_000) == "8亿（800,000,000）"
    assert module.format_token_count(1_250_000) == "1.25百万（1,250,000）"


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


def test_build_session_title_uses_first_sentence_and_200_char_limit():
    module = load_module()

    assert module.build_session_title("第一句。第二句", "fallback") == "第一句。"
    assert module.build_session_title("\n\n第一行   标题\n第二行", "fallback") == "第一行 标题 第二行"
    assert module.build_session_title("甲" * 240, "fallback") == "甲" * 200


def test_collect_usage_report_includes_session_title_and_totals(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 7, 3, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 7, 4, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    report = module.collect_usage_report(start_local, end_local)
    session = report["sessions"]["55555555-5555-4555-8555-555555555555"]

    assert session["title"] == "2026年7月3日这是第一行标题，会被截取并保留到句号。"
    assert session["cwd"] == "/workspace/usage-july-3"
    assert session["input_tokens"] == 50_000_000
    assert session["cached_input_tokens"] == 15_000_000
    assert session["output_tokens"] == 3_000_000


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
    assert sessions["019f075e-3dfc-7d42-b7e9-e9ceec7f97a6"]["title"] == "帮我统计今天的 token 使用并解释异常原因。"
    assert sessions["019f098e-5c62-7622-8fba-d1c445b9010f"]["title"] == "你是软件工厂里的增长研究负责人。"
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

    assert report["sessions"]["019f09d2-c8b8-7a20-af8a-75ba2ed310dd"]["title"] == "你是软件工厂里的增长研究负责人。"
    assert report["sessions"]["019f0ae1-adae-7370-a503-81392118d281"]["title"] == "请分析6月28日 session 标题异常并输出结论。"


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
    assert "三、Session 明细" not in output
    assert "三、Session 明细" in detail_text
    assert "3.1 2026年7月3日这是第一行标题，会被截取并保留到句号。" in detail_text


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
    assert "活跃天数：7" in output
    assert "总Token：8.54亿（854,302,465）" in output
    assert "总成本：$2,324.90（部分模型未计价）" in output
    assert "2026-07-04 | 总Token 8亿（800,000,000） | 成本 $2,187.50" in output
    assert "# 最近Token与成本明细" in detail_text
    assert "| 2026-07-04 | 8亿（800,000,000） | $2,187.50 |" in detail_text


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
    assert "| 2026-07-04 | 8亿（800,000,000） | $2,187.50 |" in detail_text


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

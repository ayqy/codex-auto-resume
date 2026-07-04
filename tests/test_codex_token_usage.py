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


def shanghai_day(module, year: int, month: int, day: int):
    start_local = datetime(year, month, day, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(year, month, day + 1, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    return start_local, end_local


def test_collect_usage_handles_null_info_and_non_string_function_output(monkeypatch, codex_home):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    start_local = datetime(2026, 6, 27, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))
    end_local = datetime(2026, 6, 28, 0, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

    results = module.collect_usage(start_local, end_local)

    assert results["gpt-5.4-mini"]["input_tokens"] == 50
    assert results["gpt-5.4-mini"]["cached_input_tokens"] == 20
    assert results["gpt-5.4-mini"]["output_tokens"] == 7


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


def test_build_session_title_uses_first_sentence_and_200_char_limit():
    module = load_module()

    assert module.build_session_title("第一句。第二句", "fallback") == "第一句。"
    assert module.build_session_title("\n\n第一行   标题\n第二行", "fallback") == "第一行 标题"
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

    assert module.main() == 0
    output = capsys.readouterr().out

    assert "日期：2026-07-04" in output
    assert "总Token：8亿（800,000,000）" in output
    assert detail_file.exists()


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
    assert "只能选择一种时间范围输入方式" in err

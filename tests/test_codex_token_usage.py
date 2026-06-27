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


def test_main_today_summary_only_succeeds(monkeypatch, codex_home, tmp_path, capsys):
    module = load_module()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    detail_file = tmp_path / "detail.txt"
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "codex_token_usage.py",
            "--today",
            "--tz",
            "Asia/Shanghai",
            "--summary-only",
            "--detail-file",
            str(detail_file),
        ],
    )
    fake_now = datetime(2026, 6, 27, 12, 0, 0, tzinfo=module.ZoneInfo("Asia/Shanghai"))

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
    assert "今日 Token 使用总结 2026-06-27" in output
    assert "明细文件:" in output
    assert detail_file.exists()


def test_main_missing_sessions_returns_error(monkeypatch, tmp_path, capsys):
    module = load_module()
    missing_home = tmp_path / "missing_home"
    missing_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(missing_home))
    monkeypatch.setattr(module.sys, "argv", ["codex_token_usage.py", "--today"])

    assert module.main() == 1
    err = capsys.readouterr().err
    assert "session directory not found" in err

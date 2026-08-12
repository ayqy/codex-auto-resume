from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_script_module(name):
    scripts_dir = ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(name, scripts_dir / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_script_module("official_usage_fallback")


def test_safe_screen_strips_terminal_sequences(module):
    value = module.safe_screen(b"\x1b[31mUse this reset?\x1b[0m\r\nYes, use reset")
    assert "Use this reset?" in value
    assert "Yes, use reset" in value
    assert "\x1b" not in value


def test_canonical_text_tolerates_terminal_redraw_spacing(module):
    assert module.canonical_text("Redeem usage limit reset") in module.canonical_text("Redeemusagelimitreset")


def test_execute_rejected_before_fallback_without_starting_tui(module, tmp_path, monkeypatch):
    operation = tmp_path / "operation.json"
    operation.write_text(json.dumps({
        "credit_id": "locked",
        "expires_at": 300,
        "fallback_at": 200,
        "authorize_consume": True,
    }))
    monkeypatch.setattr(module, "validate_locked_credit", lambda *_args: None)
    monkeypatch.setattr(module.time, "time", lambda: 100)
    with pytest.raises(module.ResetTransportError) as error:
        module.run_usage_flow(True, operation)
    assert error.value.category == "too-early"


def test_dry_run_reaches_confirmation_and_selects_safe_default(module, tmp_path, monkeypatch, capsys):
    operation = tmp_path / "operation.json"
    operation.write_text(json.dumps({"credit_id": "locked", "expires_at": 300, "fallback_at": 200}))
    monkeypatch.setattr(module, "validate_locked_credit", lambda *_args: None)
    sent = []

    class FakeTui:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait_for(self, *needles, timeout=None): return " ".join(needles)
        def wait_for_any(self, needles, timeout=None): return needles[0]
        def send(self, value): sent.append(value)

    monkeypatch.setattr(module, "CodexTui", FakeTui)
    assert module.run_usage_flow(False, operation) == 0
    assert sent == [b"/usage\r", b"\x1b[B\r", b"\r", b"\r"]
    assert "POST requests=0" in capsys.readouterr().out


def test_execute_selects_yes_only_after_confirmation(module, tmp_path, monkeypatch):
    operation = tmp_path / "operation.json"
    operation.write_text(json.dumps({
        "credit_id": "locked", "expires_at": 300, "fallback_at": 100, "authorize_consume": True,
    }))
    monkeypatch.setattr(module, "validate_locked_credit", lambda *_args: None)
    monkeypatch.setattr(module.time, "time", lambda: 200)
    sent = []

    class FakeTui:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def wait_for(self, *needles, timeout=None): return " ".join(needles)
        def wait_for_any(self, needles, timeout=None): return needles[0]
        def send(self, value): sent.append(value)

    monkeypatch.setattr(module, "CodexTui", FakeTui)
    assert module.run_usage_flow(True, operation) == 0
    assert sent[-1] == b"\x1b[A\r"

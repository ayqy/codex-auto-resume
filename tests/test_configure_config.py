from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "configure_config.py"


def load_module():
    spec = importlib.util.spec_from_file_location("configure_config", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_module()


def test_load_config_returns_defaults_when_missing(module, tmp_path):
    config = module.load_config(tmp_path / "config.json")

    assert config == {
        "proxy": {
            "http": "",
            "https": "",
            "all": "",
        },
        "workat": [],
        "resume": {
            "mode": "interactive",
        },
    }


def test_parse_sections_defaults_to_proxy_and_workat(module):
    assert module.parse_sections([]) == ["proxy", "workat", "resume"]


def test_parse_sections_rejects_removed_model_section(module):
    with pytest.raises(ValueError, match="unsupported section: model"):
        module.parse_sections(["model"])


def test_main_updates_proxy_section_with_all_proxy(module, monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(module, "get_config_path", lambda: config_path)
    answers = iter([
        "http://127.0.0.1:1087",
        "http://127.0.0.1:1087",
        "socks5://127.0.0.1:1080",
        "10:30,14:00",
        "silent",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert module.main([]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {
        "proxy": {
            "http": "http://127.0.0.1:1087",
            "https": "http://127.0.0.1:1087",
            "all": "socks5://127.0.0.1:1080",
        },
        "workat": ["10:30", "14:00"],
        "resume": {
            "mode": "silent",
        },
    }


def test_main_reads_current_values_and_echoes_proxy_prompts(module, monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "http://127.0.0.1:1087",
                    "https": "http://127.0.0.1:1087",
                    "all": "socks5://127.0.0.1:1080",
                },
                "workat": ["14:00", "10:30"],
                "resume": {
                    "mode": "interactive",
                    "model": "legacy-value",
                    "effort": "legacy-value",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "get_config_path", lambda: config_path)
    answers = iter(["", "", "", "", ""])

    def fake_input(prompt=""):
        print(prompt, end="")
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)

    assert module.main([]) == 0

    output = capsys.readouterr().out
    assert "HTTP proxy [http://127.0.0.1:1087]" in output
    assert "HTTPS proxy [http://127.0.0.1:1087]" in output
    assert "ALL_PROXY [socks5://127.0.0.1:1080]" in output
    assert "Workat times (HH:MM, comma-separated) [10:30,14:00]" in output
    assert "Resume mode (interactive/silent) [interactive]" in output
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {
        "proxy": {
            "http": "http://127.0.0.1:1087",
            "https": "http://127.0.0.1:1087",
            "all": "socks5://127.0.0.1:1080",
        },
        "workat": ["10:30", "14:00"],
        "resume": {
            "mode": "interactive",
            "model": "legacy-value",
            "effort": "legacy-value",
        },
    }


def test_main_updates_workat_section_sorts_and_dedupes(module, monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(module, "get_config_path", lambda: config_path)
    answers = iter(["14:00,10:30,14:00"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert module.main(["workat"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["workat"] == ["10:30", "14:00"]


def test_main_clears_workat_section_with_dash(module, monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "",
                    "https": "",
                    "all": "",
                },
                "workat": ["10:30", "14:00"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "get_config_path", lambda: config_path)
    answers = iter(["-"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert module.main(["workat"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["workat"] == []


def test_emit_shell_runtime_sets_and_clears_all_proxy(module):
    output = module.emit_shell_runtime(
        {
            "proxy": {
                "http": "http://127.0.0.1:1087",
                "https": "http://127.0.0.1:1087",
                "all": "socks5://127.0.0.1:1080",
            }
        }
    )

    assert "unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY" in output
    assert "export http_proxy=http://127.0.0.1:1087" in output
    assert "export ALL_PROXY=socks5://127.0.0.1:1080" in output


def test_main_updates_resume_section(module, monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(module, "get_config_path", lambda: config_path)
    answers = iter(["silent"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    assert module.main(["resume"]) == 0

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["resume"]["mode"] == "silent"

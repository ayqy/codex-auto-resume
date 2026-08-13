from __future__ import annotations

import importlib.util
import json
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
    return load_script_module("reset_credit_workflow")


def reset_credit(module, credit_id, expires):
    return module.ResetCredit(credit_id, "codexRateLimits", "available", 100, expires)


def snapshots(module, credits):
    snapshot = module.manager.ResetSnapshot(len(credits), tuple(credits), "direct-get")
    app = module.manager.ResetSnapshot(len(credits), tuple(credits), "app-server")
    return module.manager.CrossValidationResult(app, snapshot)


def answers(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


def test_prepare_checks_macos_builtins_without_package_install(module, tmp_path, monkeypatch):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o700)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "secret"}}))
    rescue = tmp_path / "reset_credit_rescue.py"
    rescue.write_text("rescue")
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "AUTH_PATH", auth)
    monkeypatch.setattr(module, "BUNDLED_RESCUE_PATH", rescue)
    monkeypatch.setattr(module, "CAFFEINATE_PATH", Path("/bin/sh"))
    monkeypatch.setattr(module, "LAUNCHCTL_PATH", Path("/bin/sh"))

    class Result:
        returncode = 0
        stdout = "codex-cli test\n"

    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: Result())
    output = []
    assert module.prepare(str(codex), output.append) == str(codex.resolve())
    assert any("macOS 自带组件" in line for line in output)


def test_validate_auth_uses_runtime_auth_path(module, tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "secret"}}))
    monkeypatch.setattr(module, "AUTH_PATH", auth)
    module.validate_auth()


def test_resolve_codex_bin_preserves_symlink_entrypoint(module, tmp_path):
    executable = tmp_path / "codex-real"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    entrypoint = tmp_path / "codex"
    entrypoint.symlink_to(executable)

    assert module.resolve_codex_bin(str(entrypoint)) == str(entrypoint)


def test_prepare_fails_with_actionable_error_when_caffeinate_missing(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "CAFFEINATE_PATH", tmp_path / "missing")
    monkeypatch.setattr(module, "LAUNCHCTL_PATH", Path("/bin/sh"))
    with pytest.raises(module.ResetTransportError) as error:
        module.prepare("/bin/sh", lambda _line: None)
    assert error.value.category == "missing-system-component"
    assert "macOS 默认系统组件" in str(error.value)


def test_credit_choice_and_execution_mode_have_intuitive_defaults(module):
    first = reset_credit(module, "first", 2_000_000_000)
    second = reset_credit(module, "second", 2_000_010_000)
    output = []
    assert module.choose_credit([first, second], answers("2"), output.append) == second
    assert module.choose_execution_mode(second, now=100, input_fn=answers(""), output_fn=output.append) == "scheduled"
    assert module.choose_execution_mode(second, now=100, input_fn=answers("2"), output_fn=output.append) == "immediate"


def test_workflow_cancel_never_arms_or_installs(module, monkeypatch):
    selected = reset_credit(module, "selected", 2_000_000_000)
    monkeypatch.setattr(module, "prepare", lambda *_args: "/bin/codex")
    monkeypatch.setattr(module, "current_operation", lambda: None)
    monkeypatch.setattr(module, "default_cross_validate", lambda **_kwargs: snapshots(module, [selected]))
    forbidden = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("forbidden mutation"))
    monkeypatch.setattr(module.manager, "arm_selected_credit", forbidden)
    output = []
    assert module.run_workflow(
        input_fn=answers("", "", "n"),
        output_fn=output.append,
        install_fn=forbidden,
    ) == 0
    assert any("POST requests=0" in line for line in output)
    assert any("已取消" in line for line in output)


def test_scheduled_workflow_arms_selected_card_and_installs(module, monkeypatch):
    first = reset_credit(module, "first", 2_000_000_000)
    selected = reset_credit(module, "selected", 2_000_010_000)
    monkeypatch.setattr(module, "prepare", lambda *_args: "/bin/codex")
    monkeypatch.setattr(module, "current_operation", lambda: None)
    monkeypatch.setattr(module, "default_cross_validate", lambda **_kwargs: snapshots(module, [first, selected]))
    captured = []

    def arm(codex_bin, credit, mode, authorize_consume=True):
        captured.append((codex_bin, credit.id, mode, authorize_consume))
        return module.manager.build_operation(credit, 2, True, now=100, execution_mode=mode)

    monkeypatch.setattr(module.manager, "arm_selected_credit", arm)
    installed = []
    output = []
    assert module.run_workflow(
        input_fn=answers("2", "1", "y"),
        output_fn=output.append,
        install_fn=lambda: installed.append(True) or 0,
    ) == 0
    assert captured == [("/bin/codex", "selected", "scheduled", True)]
    assert installed == [True]
    assert any("已定时" in line for line in output)


def test_immediate_workflow_tracks_terminal_state(module, monkeypatch):
    selected = reset_credit(module, "selected", 2_000_000_000)
    monkeypatch.setattr(module, "prepare", lambda *_args: "/bin/codex")
    monkeypatch.setattr(module, "current_operation", lambda: None)
    monkeypatch.setattr(module, "default_cross_validate", lambda **_kwargs: snapshots(module, [selected]))
    operation = module.manager.build_operation(selected, 1, True, now=100, execution_mode="immediate")
    monkeypatch.setattr(module.manager, "arm_selected_credit", lambda *_args, **_kwargs: operation)
    waited = []
    assert module.run_workflow(
        input_fn=answers("", "2", "yes"),
        output_fn=lambda _line: None,
        install_fn=lambda: 0,
        wait_fn=lambda **_kwargs: waited.append(True) or 0,
    ) == 0
    assert waited == [True]


def test_existing_active_operation_is_reused_without_query_or_selection(module, monkeypatch):
    existing = {"status": "armed", "authorize_consume": True, "trigger_at": 100, "attempts": []}
    monkeypatch.setattr(module, "prepare", lambda *_args: "/bin/codex")
    monkeypatch.setattr(module, "current_operation", lambda: existing)
    monkeypatch.setattr(
        module,
        "default_cross_validate",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not query a new card")),
    )
    installed = []
    assert module.run_workflow(output_fn=lambda _line: None, install_fn=lambda: installed.append(True) or 0) == 0
    assert installed == [True]


def test_makefile_exposes_only_one_public_reset_target():
    text = (ROOT / "Makefile").read_text()
    assert "reset:" in text
    for old in ("reset-query:", "reset-doctor:", "reset-dry-run:", "reset-arm:", "reset-watch:", "reset-status:", "reset-install:"):
        assert old not in text


def test_public_status_includes_execution_mode(module):
    credit = reset_credit(module, "selected", 2_000_000_000)
    operation = module.manager.build_operation(credit, 1, True, now=100, execution_mode="immediate")
    assert module.manager.public_operation(operation)["execution_mode"] == "immediate"


def test_main_rejects_extra_arguments_without_starting_workflow(module, monkeypatch):
    monkeypatch.setattr(
        module,
        "run_workflow",
        lambda: (_ for _ in ()).throw(AssertionError("workflow must not start")),
    )
    assert module.main(["unexpected"]) == 2

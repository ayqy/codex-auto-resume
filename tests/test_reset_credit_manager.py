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
def transport():
    return load_script_module("codex_reset_transport")


@pytest.fixture
def module():
    return load_script_module("reset_credit_manager")


def credit(transport, status="available", expires=2_000_000_000):
    return transport.ResetCredit("opaque-full-credit-id", "codexRateLimits", status, 100, expires)


def snapshots(transport, status="available", count=1, expires=2_000_000_000):
    item = credit(transport, status=status, expires=expires)
    app = transport.ResetSnapshot(count, (item,), "app-server")
    direct = transport.ResetSnapshot(count, (item,), "direct-get")
    return transport.CrossValidationResult(app, direct)


def redirect_paths(module, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(module, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(module, "PRIVATE_OPERATION_PATH", runtime / "private-operation.json")
    monkeypatch.setattr(module, "STATUS_PATH", runtime / "status.json")
    monkeypatch.setattr(module, "EVENT_LOG_PATH", runtime / "events.log")
    monkeypatch.setattr(module, "LOCK_PATH", runtime / "manager.lock")
    return runtime


def test_build_operation_uses_exact_timeline_and_persistent_idempotency(module, transport):
    operation = module.build_operation(credit(transport, expires=10000), 1, True, now=1)
    assert operation["trigger_at"] == 6400
    assert operation["fallback_at"] == 7000
    assert operation["source_rescue_at"] == 7120
    assert operation["target_at"] == 9400
    assert operation["precheck_at"] == [5500, 6280]
    assert operation["authorize_consume"] is True
    assert operation["credit_id"] == "opaque-full-credit-id"
    assert len(operation["idempotency_key"]) == 36


def test_public_state_never_contains_sensitive_identifiers(module, transport):
    operation = module.build_operation(credit(transport), 1, True, now=1)
    value = json.dumps(module.public_operation(operation))
    assert operation["credit_id"] not in value
    assert operation["idempotency_key"] not in value
    assert "credit_id" not in value
    assert "idempotency_key" not in value


def test_atomic_private_file_has_0600_and_rearm_reuses_locked_operation(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport))
    monkeypatch.setattr(module, "epoch_now", lambda: 1000)
    assert module.arm("codex", True, 1) == 0
    first = module.read_json(module.PRIVATE_OPERATION_PATH)
    assert module.PRIVATE_OPERATION_PATH.stat().st_mode & 0o777 == 0o600
    assert module.arm("codex", True, 1) == 0
    second = module.read_json(module.PRIVATE_OPERATION_PATH)
    assert second["credit_id"] == first["credit_id"]
    assert second["idempotency_key"] == first["idempotency_key"]


def test_arm_fails_closed_on_unexpected_count(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport, count=2))
    with pytest.raises(module.ResetTransportError) as error:
        module.arm("codex", True, 1)
    assert error.value.category == "unexpected-credit-count"


def test_confirmed_consumed_accepts_redeemed_or_count_transition(module, transport):
    operation = module.build_operation(credit(transport), 1, True, now=1)
    redeemed = snapshots(transport, status="redeemed", count=0)
    assert module.confirmed_consumed(redeemed, operation, "reset")

    empty_app = transport.ResetSnapshot(0, (), "app-server")
    empty_direct = transport.ResetSnapshot(0, (), "direct-get")
    assert module.confirmed_consumed(transport.CrossValidationResult(empty_app, empty_direct), operation, "reset")


def test_redeeming_never_writes(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    module.save_operation(operation)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport, status="redeeming"))
    result = module.verify_before_write(operation, "codex")
    assert result.app_server.credits[0].status == "redeeming"
    assert operation["status"] == "verifying"


def test_uncertain_attempt_reuses_same_key_and_schedules_20_then_60(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    original_key = operation["idempotency_key"]
    module.save_operation(operation)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport))

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def consume_credit(self, *_args):
            raise module.ResetTransportError("timeout", "timed out")

    monkeypatch.setattr(module, "CodexAppServerClient", Client)
    assert module.primary_attempt(operation, "codex", 100) == "uncertain"
    assert operation["idempotency_key"] == original_key
    assert operation["next_attempt_at"] == 120
    assert module.primary_attempt(operation, "codex", 120) == "uncertain"
    assert operation["idempotency_key"] == original_key
    assert operation["next_attempt_at"] == 180


def test_explicit_nothing_to_reset_rotates_only_after_verification(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    original_key = operation["idempotency_key"]
    module.save_operation(operation)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport))

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def consume_credit(self, *_args): return "nothingToReset"

    monkeypatch.setattr(module, "CodexAppServerClient", Client)
    monkeypatch.setattr(module, "confirm_after_attempt", lambda *_args, **_kwargs: False)
    assert module.primary_attempt(operation, "codex", operation["trigger_at"]) == "nothingToReset"
    assert operation["idempotency_key"] != original_key
    assert operation["logical_attempt"] == 2


def test_fourth_explicit_noop_waits_for_ten_minute_fallback(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    operation["logical_attempt"] = 4
    module.save_operation(operation)
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport))

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def consume_credit(self, *_args): return "nothingToReset"

    monkeypatch.setattr(module, "CodexAppServerClient", Client)
    monkeypatch.setattr(module, "confirm_after_attempt", lambda *_args, **_kwargs: False)
    now = operation["trigger_at"] + 480
    assert module.primary_attempt(operation, "codex", now) == "nothingToReset"
    assert operation["next_attempt_at"] == operation["fallback_at"]


def test_dry_run_never_constructs_consume_client(module, transport, monkeypatch, capsys):
    monkeypatch.setattr(module, "default_cross_validate", lambda codex_bin="codex": snapshots(transport))

    class Forbidden:
        def __init__(self, **kwargs):
            raise AssertionError("write transport created during dry-run")

    monkeypatch.setattr(module, "CodexAppServerClient", Forbidden)
    assert module.dry_run() == 0
    output = capsys.readouterr().out
    assert "POST requests=0" in output
    assert "opaque-full-credit-id" not in output


def test_log_event_whitelists_fields(module, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    module.log_event("sample", outcome="ok", token="secret", credit_id="full-id", idempotency_key="uuid")
    value = module.EVENT_LOG_PATH.read_text()
    assert "secret" not in value
    assert "full-id" not in value
    assert "uuid" not in value


def test_successful_fallback_is_immediately_cross_validated(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    module.save_operation(operation)

    class Result:
        returncode = 0
        stdout = "submitted"
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    calls = []
    monkeypatch.setattr(module, "confirm_after_attempt", lambda *args, **kwargs: calls.append(True) or True)
    assert module.run_subprocess_fallback(operation, Path("fallback.py"), "official-tui", 100, "codex") == "completed"
    assert calls == [True]


def test_source_explicit_noop_rotates_key_to_fixed_retry(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport), 1, True, now=1)
    original_key = operation["idempotency_key"]
    module.save_operation(operation)

    class Result:
        returncode = 3
        stdout = "outcome=nothing_to_reset"
        stderr = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(module, "confirm_after_attempt", lambda *args, **kwargs: False)
    now = operation["trigger_at"] + 720
    assert module.run_subprocess_fallback(operation, Path("rescue.py"), "source-rescue", now, "codex") == "nothingToReset"
    assert operation["idempotency_key"] != original_key
    assert operation["next_attempt_at"] == operation["trigger_at"] + 1200


def test_source_retry_schedule_matches_approved_timeline(module, transport):
    operation = module.build_operation(credit(transport), 1, True, now=1)
    trigger = operation["trigger_at"]
    assert module.next_source_retry_at(operation, trigger + 720) == trigger + 1200
    assert module.next_source_retry_at(operation, trigger + 1200) == trigger + 1800
    assert module.next_source_retry_at(operation, trigger + 1800) == trigger + 2400
    assert module.next_source_retry_at(operation, trigger + 2400) == trigger + 2940


def test_expired_operation_is_terminal_success_for_launchd(module, transport, tmp_path, monkeypatch):
    redirect_paths(module, tmp_path, monkeypatch)
    operation = module.build_operation(credit(transport, expires=100), 1, True, now=1)
    operation["status"] = "expired"
    module.save_operation(operation)
    assert module.watch(once=True) == 0

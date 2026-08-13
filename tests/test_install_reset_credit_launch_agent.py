from __future__ import annotations

import plistlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_script_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_plist_has_absolute_paths_restart_policy_and_no_secrets():
    module = load_script_module("install_reset_credit_launch_agent")
    payload = module.plist_payload()
    serialized = plistlib.dumps(payload).decode()
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ProgramArguments"][0] == "/bin/bash"
    assert payload["ProgramArguments"][1].startswith("/")
    assert payload["WorkingDirectory"].startswith("/")
    assert "/Documents/" not in payload["ProgramArguments"][1]
    assert "/Documents/" not in payload["WorkingDirectory"]
    assert payload["EnvironmentVariables"]["CODEX_RESET_RUNTIME_DIR"] == str(module.RUNTIME_DIR)
    assert "CODEX_SOURCE_RESCUE_PATH" not in payload["EnvironmentVariables"]
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "credit_id" not in serialized
    assert "idempotency" not in serialized


def test_deploy_bundle_migrates_private_state_and_bundled_rescue(tmp_path, monkeypatch):
    module = load_script_module("install_reset_credit_launch_agent")
    source = tmp_path / "source"
    legacy = tmp_path / "legacy"
    bundle = tmp_path / "installed" / "app"
    runtime = tmp_path / "installed" / "state"
    (source / "scripts").mkdir(parents=True)
    legacy.mkdir()
    for name in module.SCRIPT_FILES:
        (source / "scripts" / name).write_text(name)
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "scripts" / "monitor_reset_credit_launch_agent.sh").write_text("stale")
    (legacy / "private-operation.json").write_text("{}")
    monkeypatch.setattr(module, "BASE_DIR", source)
    monkeypatch.setattr(module, "LEGACY_RUNTIME_DIR", legacy)
    monkeypatch.setattr(module, "BUNDLE_DIR", bundle)
    monkeypatch.setattr(module, "RUNTIME_DIR", runtime)

    module.deploy_bundle()

    assert (bundle / "scripts" / "reset_credit_manager.py").is_file()
    assert (bundle / "scripts" / "reset_credit_rescue.py").read_text() == "reset_credit_rescue.py"
    assert not (bundle / "scripts" / "monitor_reset_credit_launch_agent.sh").exists()
    assert (runtime / "private-operation.json").stat().st_mode & 0o777 == 0o600


def test_install_scripts_never_depend_on_documents_source():
    module = load_script_module("install_reset_credit_launch_agent")
    sources = [ROOT / "scripts" / name for name in module.SCRIPT_FILES]
    sources.append(ROOT / "scripts" / "install_reset_credit_launch_agent.py")
    assert all("Documents/source" not in path.read_text() for path in sources)

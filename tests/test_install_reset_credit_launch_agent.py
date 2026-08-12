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
    assert "access_token" not in serialized
    assert "refresh_token" not in serialized
    assert "credit_id" not in serialized
    assert "idempotency" not in serialized

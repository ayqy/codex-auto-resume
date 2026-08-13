from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_module():
    path = ROOT / "scripts" / "reset_credit_rescue.py"
    spec = importlib.util.spec_from_file_location("reset_credit_rescue", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["reset_credit_rescue"] = module
    spec.loader.exec_module(module)
    return module


def test_bundled_rescue_dry_run_is_get_only(tmp_path, monkeypatch, capsys):
    module = load_module()
    operation = tmp_path / "operation.json"
    operation.write_text(json.dumps({
        "credit_id": "locked",
        "expires_at": 2_000_000_000,
        "idempotency_key": "request",
        "source_rescue_at": 100,
    }))
    calls = []

    def request(method, _url, _token, payload=None):
        calls.append((method, payload))
        return {
            "available_count": 1,
            "credits": [{
                "id": "locked",
                "status": "available",
                "reset_type": "codex_rate_limits",
                "expires_at": "2033-05-18T03:33:20Z",
            }],
        }

    monkeypatch.setattr(module, "access_token", lambda: "secret")
    monkeypatch.setattr(module, "request_json", request)
    assert module.run(operation, False) == 0
    assert calls == [("GET", None)]
    assert "POST requests=0" in capsys.readouterr().out


def test_repository_reset_scripts_do_not_reference_source_checkout():
    for path in (ROOT / "scripts").glob("*reset_credit*"):
        assert "Documents/source" not in path.read_text()

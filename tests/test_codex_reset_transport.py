from __future__ import annotations

import io
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_script_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def module():
    return load_script_module("codex_reset_transport")


def app_payload(status="available", expires=1786557614):
    return {
        "rateLimitResetCredits": {
            "availableCount": 1 if status == "available" else 0,
            "credits": [
                {
                    "id": "credit-secret-full-id",
                    "resetType": "codexRateLimits",
                    "status": status,
                    "grantedAt": 1783965614,
                    "expiresAt": expires,
                    "title": "Full reset",
                    "description": "Ready",
                }
            ],
        }
    }


def direct_payload(status="available", expires="2026-08-12T18:00:14Z"):
    return {
        "available_count": 1 if status == "available" else 0,
        "credits": [
            {
                "id": "credit-secret-full-id",
                "reset_type": "codex_rate_limits",
                "status": status,
                "granted_at": "2026-07-13T18:00:14Z",
                "expires_at": expires,
            }
        ],
    }


def test_normalizes_both_contracts_and_cross_validates(module):
    app = module.normalize_app_server_snapshot(app_payload())
    direct = module.normalize_direct_snapshot(direct_payload())
    module.validate_snapshot_pair(app, direct)
    assert app.available_count == direct.available_count == 1
    assert app.credits[0].expires_at == direct.credits[0].expires_at == 1786557614
    assert module.format_beijing_time(app.credits[0].expires_at) == "2026-08-13 02:00:14 CST"


def test_cross_validation_rejects_status_and_count_disagreement(module):
    app = module.normalize_app_server_snapshot(app_payload())
    direct = module.normalize_direct_snapshot(direct_payload(status="redeemed"))
    with pytest.raises(module.ResetTransportError, match="disagree"):
        module.validate_snapshot_pair(app, direct)


def test_cross_validation_accepts_official_sparse_details_and_reconciles(monkeypatch, module):
    sparse = app_payload()
    sparse["rateLimitResetCredits"]["credits"] = None
    app = module.normalize_app_server_snapshot(sparse)
    direct = module.normalize_direct_snapshot(direct_payload())
    module.validate_snapshot_pair(app, direct)

    class AppClient:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read_rate_limits(self): return app
        def refresh_auth(self): return None

    class DirectClient:
        def __init__(self, **_kwargs): pass
        def read_credits(self): return direct

    monkeypatch.setattr(module, "CodexAppServerClient", AppClient)
    monkeypatch.setattr(module, "WhamReadClient", DirectClient)
    result = module.default_cross_validate()
    assert result.app_server.available_count == 1
    assert result.app_server.credits == direct.credits


def test_sparse_app_details_still_reject_count_disagreement(module):
    sparse = app_payload()
    sparse["rateLimitResetCredits"]["credits"] = None
    app = module.normalize_app_server_snapshot(sparse)
    direct = module.normalize_direct_snapshot(direct_payload())
    direct = module.ResetSnapshot(0, direct.credits, direct.source)
    with pytest.raises(module.ResetTransportError, match="counts disagree"):
        module.validate_snapshot_pair(app, direct)


def test_selects_only_earliest_unexpired_available_codex_credit(module):
    payload = app_payload()
    payload["rateLimitResetCredits"]["availableCount"] = 4
    payload["rateLimitResetCredits"]["credits"] += [
        {"id": "later", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1, "expiresAt": 200},
        {"id": "earlier", "resetType": "codexRateLimits", "status": "available", "grantedAt": 1, "expiresAt": 150},
        {"id": "wrong", "resetType": "unknown", "status": "available", "grantedAt": 1, "expiresAt": 120},
    ]
    payload["rateLimitResetCredits"]["credits"][0]["expiresAt"] = 100
    snapshot = module.normalize_app_server_snapshot(payload)
    assert module.select_earliest_available_credit(snapshot, now_epoch=110).id == "earlier"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_wham_client_is_get_only_and_refreshes_once_after_401(module, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "secret-token"}}), encoding="utf-8")
    calls = []
    refreshed = []

    def opener(request, timeout):
        calls.append((request.method, request.full_url, request.headers.get("Authorization"), timeout))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "", {}, None)
        return FakeResponse(json.dumps(direct_payload()).encode())

    client = module.WhamReadClient(auth_path=auth, opener=opener, refresh_callback=lambda: refreshed.append(True))
    snapshot = client.read_credits()
    assert snapshot.available_count == 1
    assert refreshed == [True]
    assert [item[0] for item in calls] == ["GET", "GET"]
    assert all(item[2] == "Bearer secret-token" for item in calls)
    assert not hasattr(client, "post")


def test_invalid_json_and_429_are_sanitized(module, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "never-print-me"}}), encoding="utf-8")
    client = module.WhamReadClient(auth_path=auth, opener=lambda *_args, **_kwargs: FakeResponse(b"bad"))
    with pytest.raises(module.ResetTransportError) as invalid:
        client.read_credits()
    assert invalid.value.category == "invalid-response"
    assert "never-print-me" not in str(invalid.value)

    def rate_limited(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "secret body", {}, None)

    client = module.WhamReadClient(auth_path=auth, opener=rate_limited)
    with pytest.raises(module.ResetTransportError) as limited:
        client.read_credits()
    assert limited.value.category == "rate-limited"
    assert "secret body" not in str(limited.value)

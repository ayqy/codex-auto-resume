from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_NAMES = ["configure_config.py", "resolve_session_resume.py", "lib.sh", "run_silent_resume.sh"]
SESSION_ID = "11111111-1111-4111-8111-111111111111"


def build_app(tmp_path: Path) -> Path:
    app_dir = tmp_path / "app"
    scripts_dir = app_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in SCRIPT_NAMES:
        src = ROOT / "scripts" / name
        dest = scripts_dir / name
        shutil.copyfile(src, dest)
        dest.chmod(0o755)
    return app_dir


def write_rollout(codex_home: Path, session_id: str, rows: list[dict]) -> None:
    path = codex_home / "sessions" / "2026" / "07" / "08" / f"rollout-2026-07-08T07-15-40-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_fake_codex(bin_dir: Path, capture_path: Path) -> None:
    codex_path = bin_dir / "codex"
    codex_path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["CAPTURE_PATH"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "env": {
        "http_proxy": os.environ.get("http_proxy"),
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "https_proxy": os.environ.get("https_proxy"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "all_proxy": os.environ.get("all_proxy"),
        "ALL_PROXY": os.environ.get("ALL_PROXY"),
        "CODEX_RESUME_MODEL": os.environ.get("CODEX_RESUME_MODEL"),
        "CODEX_RESUME_EFFORT": os.environ.get("CODEX_RESUME_EFFORT"),
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")
""",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)


def run_resume_script(app_dir: Path, tmp_path: Path):
    capture_path = tmp_path / "capture.json"
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    codex_home = tmp_path / "codex-home"
    make_fake_codex(bin_dir, capture_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["CODEX_HOME"] = str(codex_home)
    env["CAPTURE_PATH"] = str(capture_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [str(app_dir / "scripts" / "run_silent_resume.sh"), SESSION_ID, str(workspace)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(capture_path.read_text(encoding="utf-8")), workspace


def test_run_silent_resume_uses_non_interactive_exec_resume_and_proxy(tmp_path):
    app_dir = build_app(tmp_path)
    codex_home = tmp_path / "codex-home"
    (app_dir / "config.json").write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "http://127.0.0.1:1087",
                    "https": "http://127.0.0.1:1087",
                    "all": "socks5://127.0.0.1:1080",
                },
                "resume": {
                    "mode": "silent",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    write_rollout(
        codex_home,
        SESSION_ID,
        [
            {
                "timestamp": "2026-07-07T23:15:42.833Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4", "effort": "medium"},
            },
            {
                "timestamp": "2026-07-07T23:44:25.963Z",
                "type": "event_msg",
                "payload": {"rate_limits": {"primary": {"used_percent": 100.0}, "secondary": {"used_percent": 95.0}}},
            },
        ],
    )

    data, workspace = run_resume_script(app_dir, tmp_path)

    assert data["argv"] == [
        "exec",
        "resume",
        "-m",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=medium",
        "-c",
        'approval_policy="never"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--skip-git-repo-check",
        SESSION_ID,
        "continue",
    ]
    assert data["cwd"] == str(workspace)
    assert data["env"]["HTTP_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["HTTPS_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["ALL_PROXY"] == "socks5://127.0.0.1:1080"
    assert data["env"]["CODEX_RESUME_MODEL"] == "gpt-5.4"
    assert data["env"]["CODEX_RESUME_EFFORT"] == "medium"

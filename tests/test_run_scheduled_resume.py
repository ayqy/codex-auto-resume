from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_NAMES = ["configure_config.py", "resolve_session_resume.py", "lib.sh", "run_scheduled_resume.sh"]
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
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT_CODE", "0")))
""",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)


def make_fake_post_resume_shell(shell_path: Path) -> None:
    shell_path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["POST_SHELL_CAPTURE_PATH"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}, ensure_ascii=False, indent=2), encoding="utf-8")
""",
        encoding="utf-8",
    )
    shell_path.chmod(0o755)


def run_resume_script(app_dir: Path, tmp_path: Path, fake_codex_exit_code: int = 0, capture_post_shell: bool = False):
    capture_path = tmp_path / "capture.json"
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    codex_home = tmp_path / "codex-home"
    make_fake_codex(bin_dir, capture_path)
    (home_dir / ".bash_profile").write_text(
        f'export PATH="{bin_dir}:$PATH"\n',
        encoding="utf-8",
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["CODEX_HOME"] = str(codex_home)
    env["SHELL"] = "/bin/bash"
    env["CAPTURE_PATH"] = str(capture_path)
    env["FAKE_CODEX_EXIT_CODE"] = str(fake_codex_exit_code)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    post_shell_capture_path = None
    if capture_post_shell:
        post_shell_capture_path = tmp_path / "post-shell.json"
        post_shell_path = tmp_path / "post-shell"
        make_fake_post_resume_shell(post_shell_path)
        env["CODEX_AUTO_RESUME_INTERACTIVE_SHELL"] = str(post_shell_path)
        env["POST_SHELL_CAPTURE_PATH"] = str(post_shell_capture_path)

    result = subprocess.run(
        [str(app_dir / "scripts" / "run_scheduled_resume.sh"), SESSION_ID, str(workspace)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    post_shell = None
    if post_shell_capture_path is not None:
        post_shell = json.loads(post_shell_capture_path.read_text(encoding="utf-8"))
    return json.loads(capture_path.read_text(encoding="utf-8")), codex_home, workspace, post_shell


def test_run_scheduled_resume_uses_proxy_and_session_model_values(tmp_path):
    app_dir = build_app(tmp_path)
    codex_home = tmp_path / "codex-home"
    (app_dir / "config.json").write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "http://127.0.0.1:1087",
                    "https": "http://127.0.0.1:1087",
                    "all": "socks5://127.0.0.1:1080",
                }
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
                "payload": {"model": "gpt-5.4", "effort": "xhigh"},
            },
            {
                "timestamp": "2026-07-07T23:37:08.915Z",
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

    data, _, workspace, _ = run_resume_script(app_dir, tmp_path)

    assert data["argv"] == [
        "resume",
        "-m",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=medium",
        "--yolo",
        SESSION_ID,
        "continue",
    ]
    assert data["cwd"] == str(workspace)
    assert data["env"]["http_proxy"] == "http://127.0.0.1:1087"
    assert data["env"]["HTTP_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["https_proxy"] == "http://127.0.0.1:1087"
    assert data["env"]["HTTPS_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["all_proxy"] == "socks5://127.0.0.1:1080"
    assert data["env"]["ALL_PROXY"] == "socks5://127.0.0.1:1080"
    assert data["env"]["CODEX_RESUME_MODEL"] == "gpt-5.4"
    assert data["env"]["CODEX_RESUME_EFFORT"] == "medium"


def test_run_scheduled_resume_falls_back_to_defaults_without_config_or_session(tmp_path):
    app_dir = build_app(tmp_path)

    data, _, workspace, _ = run_resume_script(app_dir, tmp_path)

    assert data["argv"] == [
        "resume",
        "-m",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=medium",
        "--yolo",
        SESSION_ID,
        "continue",
    ]
    assert data["cwd"] == str(workspace)
    assert data["env"]["http_proxy"] is None
    assert data["env"]["HTTP_PROXY"] is None
    assert data["env"]["https_proxy"] is None
    assert data["env"]["HTTPS_PROXY"] is None
    assert data["env"]["all_proxy"] is None
    assert data["env"]["ALL_PROXY"] is None
    assert data["env"]["CODEX_RESUME_MODEL"] == "gpt-5.4"
    assert data["env"]["CODEX_RESUME_EFFORT"] == "medium"


def test_run_scheduled_resume_ignores_invalid_config_and_uses_session_model(tmp_path):
    app_dir = build_app(tmp_path)
    (app_dir / "config.json").write_text("{invalid json\n", encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    write_rollout(
        codex_home,
        SESSION_ID,
        [
            {
                "timestamp": "2026-07-07T23:15:42.833Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.4-mini", "effort": "medium"},
            },
            {
                "timestamp": "2026-07-07T23:44:25.963Z",
                "type": "event_msg",
                "payload": {"rate_limits": {"primary": {"used_percent": 100.0}, "secondary": {"used_percent": 95.0}}},
            },
        ],
    )

    data, _, workspace, _ = run_resume_script(app_dir, tmp_path)

    assert data["argv"] == [
        "resume",
        "-m",
        "gpt-5.4-mini",
        "-c",
        "model_reasoning_effort=medium",
        "--yolo",
        SESSION_ID,
        "continue",
    ]
    assert data["cwd"] == str(workspace)
    assert data["env"]["http_proxy"] is None
    assert data["env"]["HTTP_PROXY"] is None
    assert data["env"]["https_proxy"] is None
    assert data["env"]["HTTPS_PROXY"] is None
    assert data["env"]["all_proxy"] is None
    assert data["env"]["ALL_PROXY"] is None
    assert data["env"]["CODEX_RESUME_MODEL"] == "gpt-5.4-mini"
    assert data["env"]["CODEX_RESUME_EFFORT"] == "medium"


def test_run_scheduled_resume_returns_to_original_cwd_after_interrupt(tmp_path):
    app_dir = build_app(tmp_path)

    data, _, workspace, post_shell = run_resume_script(
        app_dir,
        tmp_path,
        fake_codex_exit_code=130,
        capture_post_shell=True,
    )

    assert data["cwd"] == str(workspace)
    assert post_shell is not None
    assert post_shell["cwd"] == str(workspace)
    assert post_shell["argv"] == ["-il"]

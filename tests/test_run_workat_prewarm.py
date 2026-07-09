from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_NAMES = ["configure_config.py", "lib.sh", "run_workat_prewarm.sh"]


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
    },
}, ensure_ascii=False, indent=2), encoding="utf-8")
""",
        encoding="utf-8",
    )
    codex_path.chmod(0o755)


def test_run_workat_prewarm_uses_fixed_exec_settings_and_proxy(tmp_path):
    app_dir = build_app(tmp_path)
    capture_path = tmp_path / "capture.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_fake_codex(bin_dir, capture_path)

    (app_dir / "config.json").write_text(
        json.dumps(
            {
                "proxy": {
                    "http": "http://127.0.0.1:1087",
                    "https": "http://127.0.0.1:1087",
                    "all": "socks5://127.0.0.1:1080",
                },
                "workat": ["10:30", "14:00"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["CAPTURE_PATH"] = str(capture_path)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        [str(app_dir / "scripts" / "run_workat_prewarm.sh")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(capture_path.read_text(encoding="utf-8"))
    assert data["argv"] == [
        "exec",
        "-m",
        "gpt-5.4-mini",
        "-c",
        "model_reasoning_effort=low",
        "-a",
        "never",
        "--ephemeral",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(app_dir / "tmp" / "prewarm-workspace"),
        "Just say Hi",
    ]
    assert data["env"]["http_proxy"] == "http://127.0.0.1:1087"
    assert data["env"]["HTTP_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["https_proxy"] == "http://127.0.0.1:1087"
    assert data["env"]["HTTPS_PROXY"] == "http://127.0.0.1:1087"
    assert data["env"]["all_proxy"] == "socks5://127.0.0.1:1080"
    assert data["env"]["ALL_PROXY"] == "socks5://127.0.0.1:1080"

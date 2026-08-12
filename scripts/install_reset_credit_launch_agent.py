#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
from pathlib import Path


LABEL = "com.ayqy.codex-auto-resume.reset-credit"
BASE_DIR = Path(__file__).resolve().parent.parent
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
INSTALL_ROOT = Path.home() / "Library" / "Application Support" / "codex-auto-resume" / "reset-credit"
BUNDLE_DIR = INSTALL_ROOT / "app"
RUNTIME_DIR = INSTALL_ROOT / "state"
RUNNER_PATH = BUNDLE_DIR / "scripts" / "run_reset_credit_manager.sh"
LOG_DIR = RUNTIME_DIR
LEGACY_RUNTIME_DIR = BASE_DIR / "tmp" / "reset-credit"
SOURCE_RESCUE_PATH = Path("/Users/pocket/Documents/source/codex/scripts/reset_credit_rescue.py")
SCRIPT_FILES = (
    "codex_reset_transport.py",
    "configure_config.py",
    "official_usage_fallback.py",
    "monitor_reset_credit_launch_agent.sh",
    "reset_credit_manager.py",
    "run_reset_credit_manager.sh",
)
STATE_FILES = (
    "private-operation.json",
    "status.json",
    "events.log",
    "source-manifest.json",
)


def _copy_file(source: Path, destination: Path, mode: int) -> None:
    if not source.is_file():
        raise RuntimeError(f"required reset-credit file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.chmod(mode)
    temporary.replace(destination)


def deploy_bundle() -> None:
    BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    BUNDLE_DIR.chmod(0o700)
    RUNTIME_DIR.chmod(0o700)
    scripts_dir = BUNDLE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.chmod(0o700)
    for name in SCRIPT_FILES:
        mode = 0o700 if name.endswith(".sh") or name == "reset_credit_manager.py" else 0o600
        _copy_file(BASE_DIR / "scripts" / name, scripts_dir / name, mode)
    _copy_file(SOURCE_RESCUE_PATH, scripts_dir / "reset_credit_rescue.py", 0o600)
    config_path = BASE_DIR / "config.json"
    if config_path.is_file():
        _copy_file(config_path, BUNDLE_DIR / "config.json", 0o600)
    if not (RUNTIME_DIR / "private-operation.json").exists():
        for name in STATE_FILES:
            source = LEGACY_RUNTIME_DIR / name
            if source.is_file():
                _copy_file(source, RUNTIME_DIR / name, 0o600)


def plist_payload() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(RUNNER_PATH)],
        "WorkingDirectory": str(BUNDLE_DIR),
        "EnvironmentVariables": {
            "CODEX_RESET_RUNTIME_DIR": str(RUNTIME_DIR),
            "CODEX_SOURCE_RESCUE_PATH": str(BUNDLE_DIR / "scripts" / "reset_credit_rescue.py"),
            "HOME": str(Path.home()),
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_DIR / "launchd.stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.stderr.log"),
        "ThrottleInterval": 10,
    }


def install(load: bool = True) -> int:
    deploy_bundle()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = plist_payload()
    temporary = PLIST_PATH.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
    temporary.replace(PLIST_PATH)
    PLIST_PATH.chmod(0o600)
    if not load:
        print(PLIST_PATH)
        return 0
    domain = f"gui/{subprocess.check_output(['/usr/bin/id', '-u'], text=True).strip()}"
    subprocess.run(["/bin/launchctl", "bootout", domain, str(PLIST_PATH)], check=False, capture_output=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(PLIST_PATH)], check=True)
    subprocess.run(["/bin/launchctl", "enable", f"{domain}/{LABEL}"], check=True)
    subprocess.run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], check=True)
    status = subprocess.run(
        ["/bin/launchctl", "print", f"{domain}/{LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        status.returncode != 0
        or f"path = {PLIST_PATH}" not in status.stdout
        or str(RUNNER_PATH) not in status.stdout
    ):
        raise RuntimeError("LaunchAgent did not load")
    print(f"loaded {domain}/{LABEL}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-only", action="store_true")
    args = parser.parse_args(argv)
    return install(load=not args.write_only)


if __name__ == "__main__":
    raise SystemExit(main())

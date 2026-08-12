#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import subprocess
from pathlib import Path


LABEL = "com.ayqy.codex-auto-resume.reset-credit"
BASE_DIR = Path(__file__).resolve().parent.parent
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
RUNNER_PATH = BASE_DIR / "scripts" / "run_reset_credit_manager.sh"
LOG_DIR = BASE_DIR / "tmp" / "reset-credit"


def plist_payload() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(RUNNER_PATH)],
        "WorkingDirectory": str(BASE_DIR),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_DIR / "launchd.stdout.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.stderr.log"),
        "ThrottleInterval": 10,
    }


def install(load: bool = True) -> int:
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
    if status.returncode != 0 or f"path = {PLIST_PATH}" not in status.stdout:
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

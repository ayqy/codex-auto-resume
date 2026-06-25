#!/usr/bin/env python3
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def applescript_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def mac_has_iterm() -> bool:
    probe = 'tell application "System Events" to return exists application process "iTerm2"'
    result = subprocess.run(
        ["osascript", "-e", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "true" in result.stdout.lower()


def open_iterm(script_path: str, session_id: str, cwd: str) -> None:
    command = f"bash {shell_quote(script_path)} {shell_quote(session_id)} {shell_quote(cwd)}"
    applescript = """
tell application "iTerm"
    activate
    if (count of windows) = 0 then
        create window with default profile
    else
        tell current window
            create tab with default profile
        end tell
    end if
    tell current session of current window
        write text %s
    end tell
end tell
""" % applescript_quote(command)
    subprocess.run(["osascript", "-"], input=applescript, text=True, check=True)


def open_terminal_app(script_path: str, session_id: str, cwd: str) -> None:
    command = f"bash {shell_quote(script_path)} {shell_quote(session_id)} {shell_quote(cwd)}"
    applescript = """
tell application "Terminal"
    activate
    do script %s
end tell
""" % applescript_quote(command)
    subprocess.run(["osascript", "-"], input=applescript, text=True, check=True)


def linux_terminal_command(script_path: str, session_id: str, cwd: str):
    command = f"bash {shell_quote(script_path)} {shell_quote(session_id)} {shell_quote(cwd)}"
    candidates = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", command]),
        ("gnome-terminal", ["gnome-terminal", "--", "bash", "-lc", command]),
        ("konsole", ["konsole", "-e", "bash", "-lc", command]),
        ("xfce4-terminal", ["xfce4-terminal", "--command", command]),
        ("xterm", ["xterm", "-e", command]),
    ]
    for binary, args in candidates:
        if shutil.which(binary):
            return args
    return None


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: open_terminal_and_resume.py <session_id> <cwd>", file=sys.stderr)
        return 1

    session_id = sys.argv[1]
    cwd = sys.argv[2]
    script_path = str(Path(__file__).with_name("run_scheduled_resume.sh").resolve())
    system = platform.system()

    try:
        if system == "Darwin":
            if mac_has_iterm():
                open_iterm(script_path, session_id, cwd)
            else:
                open_terminal_app(script_path, session_id, cwd)
            return 0

        if system == "Linux":
            cmd = linux_terminal_command(script_path, session_id, cwd)
            if not cmd:
                print("no supported terminal emulator found", file=sys.stderr)
                return 1
            subprocess.Popen(cmd)
            return 0

        print(f"unsupported platform: {system}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"failed to open terminal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

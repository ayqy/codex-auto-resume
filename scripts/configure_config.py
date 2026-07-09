#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path


VALID_SECTIONS = {"proxy", "workat"}
WORKAT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def get_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.json"


def default_config() -> dict:
    return {
        "proxy": {
            "http": "",
            "https": "",
            "all": "",
        },
        "workat": [],
    }


def normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def load_config(path: Path) -> dict:
    config = default_config()
    if not path.exists():
        return config

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.json must contain a JSON object")

    extras = {key: value for key, value in raw.items() if key not in {"proxy", "workat"}}
    config.update(extras)

    proxy = raw.get("proxy")
    if proxy is not None and not isinstance(proxy, dict):
        raise ValueError("config.proxy must be an object")
    if isinstance(proxy, dict):
        if "http" in proxy:
            config["proxy"]["http"] = normalize_text(proxy.get("http"))
        if "https" in proxy:
            config["proxy"]["https"] = normalize_text(proxy.get("https"))
        if "all" in proxy:
            config["proxy"]["all"] = normalize_text(proxy.get("all"))

    if "workat" in raw:
        config["workat"] = normalize_workat_list(raw.get("workat"))

    return config


def save_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_sections(argv: list[str]) -> list[str]:
    if not argv:
        return ["proxy", "workat"]

    sections = []
    for item in argv:
        if item not in VALID_SECTIONS:
            raise ValueError(f"unsupported section: {item}")
        sections.append(item)
    return sections


def prompt_text(label: str, current_value: str) -> str:
    suffix = current_value if current_value else ""
    raw = input(f"{label} [{suffix}]: ").strip()
    if raw == "":
        return current_value
    if raw == "-":
        return ""
    return raw


def prompt_proxy_section(config: dict) -> None:
    current_proxy = config["proxy"]
    print("Configuring proxy")
    current_proxy["http"] = prompt_text("HTTP proxy", current_proxy["http"])
    current_proxy["https"] = prompt_text("HTTPS proxy", current_proxy["https"])
    current_proxy["all"] = prompt_text("ALL_PROXY", current_proxy["all"])


def parse_workat_text(value: str) -> tuple[int, str]:
    normalized = normalize_text(value)
    match = WORKAT_RE.match(normalized)
    if not match:
        raise ValueError(f"invalid workat time: {value}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    return hour * 60 + minute, f"{hour:02d}:{minute:02d}"


def normalize_workat_list(values) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("config.workat must be an array")

    unique = {}
    for item in values:
        if not isinstance(item, str):
            raise ValueError("config.workat entries must be strings")
        _, normalized = parse_workat_text(item)
        unique[normalized] = True
    return sorted(unique.keys())


def parse_workat_input(raw: str) -> list[str]:
    parts = [segment.strip() for segment in raw.split(",")]
    values = [segment for segment in parts if segment]
    return normalize_workat_list(values)


def prompt_workat_section(config: dict) -> None:
    current_values = config.get("workat", [])
    current_text = ",".join(current_values)
    print("Configuring workat")
    while True:
        raw = input(f"Workat times (HH:MM, comma-separated) [{current_text}]: ").strip()
        if raw == "":
            return
        if raw == "-":
            config["workat"] = []
            return
        try:
            config["workat"] = parse_workat_input(raw)
            return
        except ValueError:
            print("Invalid workat format. Use HH:MM,HH:MM (example: 10:30,14:00).")


def emit_shell_runtime(config: dict) -> str:
    lines = [
        "unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY",
    ]

    http_proxy = config["proxy"]["http"]
    https_proxy = config["proxy"]["https"]
    all_proxy = config["proxy"]["all"]
    if http_proxy:
        quoted = shlex.quote(http_proxy)
        lines.append(f"export http_proxy={quoted}")
        lines.append(f"export HTTP_PROXY={quoted}")
    if https_proxy:
        quoted = shlex.quote(https_proxy)
        lines.append(f"export https_proxy={quoted}")
        lines.append(f"export HTTPS_PROXY={quoted}")
    if all_proxy:
        quoted = shlex.quote(all_proxy)
        lines.append(f"export all_proxy={quoted}")
        lines.append(f"export ALL_PROXY={quoted}")

    return "\n".join(lines) + "\n"


def usage() -> str:
    return "usage: configure_config.py [proxy] [workat] | --emit-shell-runtime"


def run_interactive(argv: list[str]) -> int:
    path = get_config_path()
    sections = parse_sections(argv)
    config = load_config(path)

    for section in sections:
        if section == "proxy":
            prompt_proxy_section(config)
        elif section == "workat":
            prompt_workat_section(config)

    save_config(path, config)
    print(f"Saved config to {path}")
    print("Run `make run` to use the updated settings.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--emit-shell-runtime" in args:
        if len(args) != 1:
            print(usage(), file=sys.stderr)
            return 2
        try:
            config = load_config(get_config_path())
        except Exception as exc:
            print(f"warning: failed to parse config.json: {exc}", file=sys.stderr)
            config = default_config()
        sys.stdout.write(emit_shell_runtime(config))
        return 0

    try:
        return run_interactive(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        print(usage(), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"failed to update config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

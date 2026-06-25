# codex-auto-resume

Small cross-platform helper for watching Codex usage-limit signals, extracting the latest affected session, and opening a terminal to resume that session after the retry window.

## Features

- Scans local Codex SQLite state and rollout logs
- Detects usage-limit events and retry timestamps
- Resolves the affected session's working directory
- Opens iTerm/Terminal on macOS or a Linux terminal emulator
- Supports `make` targets for daily token summary, watcher run, and forced resume

## Usage

```bash
make
make run
make force
```

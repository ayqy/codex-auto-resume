<div align="center">

**[ C :: A ]**

# Codex Auto-Resume

[**English**](./README.md) | [中文](./README.zh-CN.md)

[![Project Status: Active](https://img.shields.io/badge/status-active-success.svg)](https://github.com/your-repo/codex-auto-resume)
[![Python Version](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

**Never let a usage limit interrupt your flow again. This tool automatically resumes your Codex sessions for you.**

</div>

---

## What It Does

You're in the zone, deep in a coding session with Codex, and suddenly... **"You've hit your usage limit."**

Your focus is shattered. You have to remember to come back in an hour to resume your work.

**Codex Auto-Resume** solves this. It runs in the background, watches for usage limit errors, and automatically opens a new terminal to resume your session as soon as the lockout is over.

## Features

- **Automatic Session Resumption**: Detects usage limit lockouts and automatically schedules a new terminal to open right when you can resume.
- **Cross-Platform**: Works on macOS (iTerm2, Terminal.app) and Linux (gnome-terminal, etc.).
- **Usage Analytics**: Provides commands to track your token usage, costs, and active coding time.
- **Resilient**: Works even if some of the usual log files are unavailable, ensuring you don't miss a resumption.

## Getting Started

### Prerequisites

- Python 3.x
- An existing Codex installation.

### Installation

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/your-repo/codex-auto-resume.git
    cd codex-auto-resume
    ```

2.  **Make scripts executable**:
    ```bash
    make chmod
    ```

3.  **Start the watcher**:
    ```bash
    make run
    ```
    This starts the watcher in your current terminal. Keep it running to monitor your Codex sessions.

## Usage

### Main Commands

| Command      | Description                                                                          |
|--------------|--------------------------------------------------------------------------------------|
| `make run`   | **(Most important)** Starts the background watcher to monitor for usage limits and resume your session automatically. |
| `make today` | Shows a detailed report of your token usage, active time, and estimated costs for today. |
| `make usage` | Shows the same report for a specific day. (e.g., `make usage D=2026-07-03`) |
| `make recent`| Shows usage stats for the last 30 days. (e.g., `make recent N=7` for the last 7 days) |
| `make status`| Shows the current status of the watcher, including pending and triggered resume jobs. |
| `make test`  | Runs the automated tests for the project. |

### Usage Examples

-   `make today`
    > Get a summary of your usage for today.

-   `make usage D=2026-07-03`
    > Get a usage report for a specific date.

-   `make recent N=7`
    > See your usage statistics for the last 7 days.

-   `make today F=/tmp/codex-today.txt`
    > Save today's detailed report to a specific file.

<details>
<summary><b>Advanced Usage & Debugging</b></summary>

For more advanced use cases, you can use the `make debug` command or call the Python scripts directly.

#### Debug Commands

-   `make debug`: Prints a full debug dashboard.
-   `make debug DEBUG_ARGS="--debug-limit-history --days 14"`: Show recent limit history.
-   `make debug DEBUG_ARGS="--debug-session <session_id>"`: Print merged metadata for a specific session.
-   `make debug DEBUG_ARGS="--debug-schedule-once"`: Run one scheduling cycle and exit.
-   `make debug DEBUG_ARGS="--debug-force-latest"`: Force-trigger the latest detected session.

#### Direct Script Execution

You can also run the usage script directly for more control, such as specifying exact time ranges or timezones.

```bash
python3 scripts/codex_token_usage.py "2026-07-01 00:00:00" "2026-07-01 23:59:59" -z America/Los_Angeles
```

</details>

## Contributing

Contributions are welcome! Please see `CONTRIBUTING.md` for more details.

## License

Distributed under the MIT License. See `LICENSE` for more information.

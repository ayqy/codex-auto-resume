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

## Table of Contents

- [About The Project](#about-the-project)
- [Features](#features)
- [How It Works](#how-it-works)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
- [Usage](#usage)
  - [Common Commands](#common-commands)
  - [All Commands](#all-commands)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgements](#acknowledgements)

## About The Project

You're in the zone, deep in a coding session with Codex, and suddenly... **"You've hit your usage limit."**

Your focus is shattered. You have to remember to come back in an hour, find the right session, reopen the terminal, and try to piece together your train of thought.

**Codex Auto-Resume** solves this problem. It's a lightweight, fire-and-forget daemon that monitors your Codex activity. When it detects a usage limit error, it automatically schedules and triggers a new terminal session that resumes your work right where you left off, as soon as the lockout period is over.

It's designed to be a seamless, invisible assistant that keeps you productive.

## Features

- 🎯 **Automated Usage-Limit Detection**: Scans local Codex logs to detect usage-limit events and their retry timestamps.
- 🧠 **Intelligent Session Resolution**: Identifies the exact session and working directory affected by the usage limit.
- 💻 **Cross-Platform Terminal Integration**: Works with iTerm2, Terminal.app, gnome-terminal, and more.
- ⏰ **Scheduled & Manual Resumption**: Schedules a resume task automatically or allows you to force an immediate resume.
- 📊 **Token Usage Reporting**: Includes a script to summarize daily token usage and estimate costs.
- 🛡️ **Robust State Management**: Tracks processed errors and pending jobs to prevent duplicates.

## How It Works

The tool runs a small daemon in the background that follows a simple, robust workflow.

```mermaid
graph TD
    A[Start Watcher: make run] --> B{Scan Codex Logs};
    B --> C{Usage Limit Found?};
    C -- No --> B;
    C -- Yes --> D[Parse Session ID & Retry Time];
    D --> E[Schedule Resume Task for 10 min after Retry];
    E --> F{Time to Run?};
    F -- No --> E;
    F -- Yes --> G[Open New Terminal & Run `codex resume`];
```

## Getting Started

### Prerequisites

- Python 3.x
- `make` (optional, but recommended for easy command access)
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
    That's it! The watcher is now running in the background.

## Usage

### Common Commands

These are the commands you'll use most frequently.

| Command      | Description                                                                          |
|--------------|--------------------------------------------------------------------------------------|
| `make run`   | **(Recommended)** Starts the watcher in the background to continuously monitor for errors. |
| `make today` | Shows a summary of your Codex token usage and estimated costs for the current day.       |
| `make force` | Immediately triggers a resume for the most recently detected session, ignoring schedules. |
| `make status`| Displays the watcher's current state, including pending and completed jobs.            |

### All Commands

Here is a complete list of all available commands.

| Command         | Description                                                                       |
|-----------------|-----------------------------------------------------------------------------------|
| `make today`    | Show a summary of token usage for the current day.                                |
| `make run`      | Start the watcher daemon to continuously monitor for usage limit errors.          |
| `make once`     | Run the watcher a single time to detect errors and schedule a resume if needed.   |
| `make status`   | Print the internal JSON state of the watcher (pending jobs, processed errors, etc.). |
| `make force`    | Force-trigger an immediate resume for the most recently detected error session.     |
| `make test-sample`| Run a standalone test of the error-message parsing logic.                         |
| `make clean`    | Remove all temporary files, logs, and state generated by the watcher.             |
| `make chmod`    | Apply `+x` permissions to all shell scripts in the `scripts/` directory.          |


## Contributing

Contributions are what make the open source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

Please see `CONTRIBUTING.md` for details on our code of conduct, and the process for submitting pull requests to us.

## License

Distributed under the MIT License. See `LICENSE` for more information.

## Acknowledgements

- [Shields.io](https://shields.io) for the awesome badges.
- Inspired by the need to stay focused during long coding sessions.

<div align="center">

**[ C :: A ]**

# Codex Auto-Resume

[English](./README.md) | [**中文**](./README.zh-CN.md)

[![项目状态: 活跃](https://img.shields.io/badge/status-active-success.svg)](https://github.com/your-repo/codex-auto-resume)
[![Python 版本](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/)
[![许可证: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![欢迎 PR](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](./CONTRIBUTING.md)

**不要让“用量超限”再次打断您的思路。本工具会自动为您恢复 Codex 会话。**

</div>

---

## 功能简介

您是否正在编程的“心流”状态中，与 Codex 深度协作，却突然看到……**“您已达到使用上限。”**

您的专注力瞬间被打破。您必须记着在一小时后回来，才能继续您的工作。

**Codex Auto-Resume** 正是为解决此问题而生。它会在后台运行，监控您的 Codex 使用情况，一旦检测到用量超限，便会在锁定时间结束后，自动为您打开一个新的终端会话，让您从中断的地方无缝继续。

## 主要特性

- **自动会话恢复**: 检测用量超限锁定，并在您可以恢复工作时，自动安排打开一个新的终端。
- **跨平台支持**: 支持 macOS (iTerm2, Terminal.app) 和 Linux (gnome-terminal 等)。
- **用量分析**: 提供命令来追踪您的 token 使用量、开销和活跃编码时间。
- **高容错性**: 即使部分日志文件不可用，它依然能正常工作，确保您不会错过任何一次恢复机会。

## 快速上手

### 环境要求

- Python 3.x
- 已安装 Codex。

### 安装步骤

1.  **克隆仓库**:
    ```bash
    git clone https://github.com/your-repo/codex-auto-resume.git
    cd codex-auto-resume
    ```

2.  **赋予脚本执行权限**:
    ```bash
    make chmod
    ```

3.  **配置终端代理和 workat 时间表**:
    ```bash
    make config
    ```
    该命令会以交互方式把终端代理、可选的 `workat` 时间表以及 resume 模式写入 `config.json`。代理包括 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY`。`workat` 支持一个或多个 `HH:MM` 时间点，例如 `10:30,14:00`。resume 模式支持 `interactive` 和 `silent`。

4.  **启动监控**:
    ```bash
    make run
    ```
    此命令会在您当前的终端中启动监控进程。请保持该终端运行，以便持续监控您的 Codex 会话。

## 使用说明

### 主要命令

| 命令 | 描述 |
|---|---|
| `make config` | 交互式配置 `config.json` 中的代理、`workat` 和 resume 模式。 |
| `make config proxy` | 配置 `config.json` 中的 `HTTP_PROXY`、`HTTPS_PROXY` 和 `ALL_PROXY`。 |
| `make config workat` | 使用 `HH:MM` 格式配置 `config.json` 中一个或多个每日 `workat` 时间点。 |
| `make config resume` | 配置 auto resume 使用交互式终端还是静默后台模式。 |
| `make run` | **(最重要)** 启动后台监控进程，持续监测用量限制并自动为您恢复会话。控制台输出会刻意保持精简，详细诊断继续写入 `tmp/logs/watcher.log`。 |
| `make today` | 显示您今天的 token 使用量、活跃时间及预估开销的详细报告。 |
| `make usage` | 显示指定某一天的同样内容的报告。(例如: `make usage D=2026-07-03`) |
| `make recent`| 显示过去 30 天的用量统计。(例如: `make recent N=7` 显示过去 7 天) |
| `make status`| 显示监控程序的当前状态，包括等待中和已触发的恢复任务。 |
| `make test` | 运行项目中的自动化测试。 |

### 使用示例

-   `make config`
    > 先配置代理，再按需配置一个或多个 `workat` 时间点。

-   `make config proxy`
    > 更新自动恢复终端使用的 HTTP/HTTPS/ALL 代理。

-   `make config workat`
    > 仅更新 `workat` 时间表，例如 `10:30,14:00`。

-   `make config resume`
    > 在 `interactive` 终端模式和 `silent` 非交互模式之间切换 auto resume。

-   `make today`
    > 获取您今天的用量摘要。

-   `make usage D=2026-07-03`
    > 获取指定日期的用量报告。

-   `make recent N=7`
    > 查看您过去 7 天的用量统计。

-   `make today F=/tmp/codex-today.txt`
    > 将今天的详细报告保存到指定文件。

自动恢复现在会先从目标会话的 rollout 日志中恢复原会话使用的模型和推理强度，再执行 `codex resume`，以避免切换模型导致缓存连续性丢失。

如果某个已调度的会话后来收到了新的正常 AI 消息，watcher 会在下一次轮询时自动取消这个 pending schedule，而不是等到计划触发时间才跳过。

如果配置了 `workat`，`make run` 会为每个配置时间只保留一个最近即将到来的 `workat - 4 小时` 静默预热探针。该探针固定使用非交互 `codex exec`、模型 `gpt-5.4-mini`、`low` 推理强度以及提示词 `Just say Hi`，以最小 token 成本刷新滚动窗口，并且不会弹出终端打扰用户。

如果当前存在 pending resume，watcher 在每次轮询时还会复用同一套探针提前检测额度是否已经恢复。探针自己遇到的 limit error 会被忽略，不会反过来积累新的 auto resume 任务。

当 `make run` 运行正常且本轮没有用户需要关注的变化时，控制台只会输出一行简短摘要。如果需要查看完整内部轨迹，请检查 `tmp/logs/watcher.log`；`tmp/state.json` 仍然是 pending / triggered 任务的最终状态来源。

<details>
<summary><b>高级用法与调试</b></summary>

对于更高级的使用场景，您可以使用 `make debug` 命令或直接调用 Python 脚本。

#### 调试命令

-   `make debug`: 打印完整的调试面板。
-   `make debug DEBUG_ARGS="--debug-limit-history --days 14"`: 显示近期的用量限制历史。
-   `make debug DEBUG_ARGS="--debug-session <session_id>"`: 打印指定会话的合并后元数据。
-   `make debug DEBUG_ARGS="--debug-schedule-once"`: 仅运行一次调度周期后退出。
-   `make debug DEBUG_ARGS="--debug-force-latest"`: 强制触发最新检测到的会话。

#### 直接执行脚本

您也可以直接运行用量脚本以获得更多控制，例如指定精确的时间范围或时区。

```bash
python3 scripts/codex_token_usage.py "2026-07-01 00:00:00" "2026-07-01 23:59:59" -z America/Los_Angeles
```

</details>

## 参与贡献

我们欢迎任何形式的贡献！更多详情请参阅 `CONTRIBUTING.md`。

## 许可证

本项目基于 MIT 许可证分发。详情请见 `LICENSE` 文件。

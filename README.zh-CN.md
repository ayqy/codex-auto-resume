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

## 目录

- [关于本项目](#关于本项目)
- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [调度规则](#调度规则)
- [开始使用](#开始使用)
  - [先决条件](#先决条件)
  - [安装步骤](#安装步骤)
- [使用方法](#使用方法)
  - [常用命令](#常用命令)
  - [所有命令](#所有命令)
- [贡献代码](#贡献代码)
- [许可证](#许可证)
- [致谢](#致谢)

## 关于本项目

您是否正在编程的“心流”状态中，与 Codex 深度协作，却突然看到……**“您已达到使用上限。”**

您的专注力瞬间被打破。您必须记着在一小时后回来，找到正确的会话，重新打开终端，然后努力拼凑回刚才的思路。

**Codex Auto-Resume** 正是为解决此问题而生。它是一个轻量级的、即开即用的守护程序，负责监控您的 Codex 活动。当检测到用量超限错误时，它会自动安排并在锁定时间结束后，触发一个新的终端会话，让您从中断的地方无缝继续。

它旨在成为一个无感、高效的助手，让您保持高效的生产力。

## 功能特性

- 🎯 **自动检测用量超限**: 扫描本地 Codex 日志，实时检测用量超限事件及其重试时间戳。
- 🧠 **智能会话解析**: 准确识别受影响的会话 ID 和项目工作目录。
- 💻 **跨平台终端集成**: 支持 iTerm2、Terminal.app、gnome-terminal 等多种终端应用。
- ⏰ **自动恢复与可调试恢复**: 自动安排恢复任务，并在需要排查时提供调试调度与调试恢复能力。
- 📊 **Token 用量报告**: 内置脚本，可汇总每日 Token 使用量并估算开销。
- 🛡️ **稳健的状态管理**: 精准追踪已处理的错误和待处理的任务，防止重复或遗漏。

## 工作原理

本工具在后台运行一个小型的守护进程，遵循一个简单而稳健的工作流。

```mermaid
graph TD
    A[启动监控: make run] --> B{扫描 Codex 日志};
    B --> C{发现用量超限?};
    C -- 否 --> B;
    C -- 是 --> D[解析会话 ID 和重试时间];
    D --> E[安排恢复任务(重试时间后10分钟)];
    E --> F{是否到达执行时间?};
    F -- 否 --> E;
    F -- 是 --> G[打开新终端并运行 `codex resume`];
```

## 调度规则

监控器按照下面 3 条规则维护 `pending_jobs`:

1. 不同 `session_id` 的未过期用量超限事件会并存调度；同一个 `session_id` 只保留 1 条活跃任务，始终选择该会话当前最晚、最强的候选。
2. 二级总额度窗口事件优先于普通的一级 `retry at` 事件。实现上，`secondary.used_percent == 100` 以及 `credits exhausted while secondary active` 都会被视为 `global_window` 候选。
3. 一旦监听到 `global_window` 候选，所有尚未触发的会话恢复任务都会统一改到该候选的重试时间后 10 分钟执行，因为一级窗口在二级总额度耗尽后已不再有效。

## 开始使用

### 先决条件

- Python 3.x
- `make` (可选，但推荐使用以便于执行命令)
- 已安装的 Codex.

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

3.  **启动监控程序**:
    ```bash
    make run
    ```
    就是这样！监控程序现在已在后台运行。

## 使用方法

### 常用命令

这些是您最常用的命令。

| 命令         | 描述                                                       |
|--------------|------------------------------------------------------------|
| `make run`   | **(推荐)** 启动后台守护进程，持续监控错误。                |
| `make today` | 显示当天 token 汇总、总 Token、session 明细和预估费用。    |
| `make usage` | 与 `make today` 相同；可通过 `D=YYYY-MM-DD` 指定查询日期。 |
| `make debug` | 输出近期 limit 事件与目标调度状态；可通过 `DEBUG_ARGS` 切换到具体 debug 子流程。 |
| `make status`| 显示监控程序的当前状态，包括待处理和已完成的任务。         |
| `make test`  | 运行基于脱敏真实样本构建的自动化单测。                     |

### 所有命令

这是所有可用命令的完整列表。

| 命令             | 描述                                                           |
|------------------|----------------------------------------------------------------|
| `make today`     | 显示当天 token 汇总、总 Token、session 明细和预估费用。        |
| `make usage`     | 与 `make today` 相同；可通过 `D=YYYY-MM-DD` 指定查询日期。     |
| `make run`       | 启动守护进程，以持续监控用量超限错误。                         |
| `make status`    | 打印监控程序的内部 JSON 状态（待处理任务、已处理错误等）。     |
| `make debug`     | 默认输出最近 7 天的 limit 事件、候选结果和目标 pending jobs。  |
| `make test`      | 运行自动化单测与脱敏 fixtures。                                |
| `make clean`     | 删除监控程序生成的所有临时文件、日志和状态。                   |
| `make chmod`     | 为 `scripts/` 目录下的所有 shell 脚本赋予 `+x` 执行权限。      |

### Debug 子命令

`make debug` 支持通过 `DEBUG_ARGS` 进入具体 debug 子流程：

- `make debug DEBUG_ARGS="--debug-limit-history --days 14"` 查看近期 limit 历史与调度决策。
- `make debug DEBUG_ARGS="--debug-session <session_id>"` 查看单个会话的元数据与候选。
- `make debug DEBUG_ARGS="--debug-schedule-once"` 仅执行一轮调试调度，不启动常驻进程。
- `make debug DEBUG_ARGS="--debug-force-latest"` 对最近检测到的会话执行调试恢复。

### Usage 示例

- `make today` 输出当天的 token 汇总、模型汇总和 session 级明细。
- `make usage D=2026-07-03` 输出 2026-07-03 当天的同类报告。


## 贡献代码

开源社区因贡献而生生不息，我们衷心感谢您所做的任何贡献。

有关行为准则和提交拉取请求（Pull Request）的详细信息，请参阅 `CONTRIBUTING.md`。

## 许可证

本项目基于 MIT 许可证分发。详情请见 `LICENSE` 文件。

## 致谢

- [Shields.io](https://shields.io) 提供的精美徽章。
- 灵感来源于在长时间编码中保持专注的需求。

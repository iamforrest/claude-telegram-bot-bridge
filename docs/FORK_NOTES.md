# Fork 说明

本仓库是 [terranc/claude-telegram-bot-bridge](https://github.com/terranc/claude-telegram-bot-bridge) 的 fork，维护者：[@iamforrest](https://github.com/iamforrest)。记录了相对上游的差异、为什么做这些改动，以及选型背景。

## 为什么选这个 bot（而不是 OpenClaw 或其他）

对比过 `six-ddc/ccbot`、`RichardAtCT/claude-code-telegram`、OpenClaw 等，`terranc` 系列胜在：

- **活跃度高** —— 上游基本每周有更新
- **`PROJECT_ROOT` 沙箱在 bot 层独立实现，不依赖 `--dangerously-skip-permissions`**。sandbox 内工具自动放行，sandbox 外触发 Telegram 按钮确认，半 YOLO 模式
- **多 bot 实例支持**（见 `docs/OPERATIONS.md` "多 bot 并行"）
- **国内用户友好**：原生支持火山 ASR、proxy 配置齐全
- **macOS launchd 集成**（`./start.sh --install`）
- **轻量**：跑在你本机、用你本机的 Claude 登录态；Anthropic 视角和直接敲 `claude` 没差别，不违反 ToS，也不是 OpenClaw 那种 web 订阅层

## 本 fork 相对 terranc 上游的差异

### 1. 迁移到 `claude-agent-sdk`（2026-04-19）

上游 `terranc` 仍在用 `claude-code-sdk 0.0.25`，该包在 PyPI 已停更，而 Claude CLI v2.1.x 起开始发 `rate_limit_event` 消息，老 SDK 会抛 `Unknown message type` 错误在 Telegram 显示红叉。

本 fork 已迁移到官方新包 [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) (≥ 0.1.63)：
- `ClaudeCodeOptions` → `ClaudeAgentOptions`
- `append_system_prompt` 字段 → `system_prompt={"type":"preset","preset":"claude_code","append":...}`
- 原生支持 `RateLimitEvent`，不需要 monkey patch
- 新增 `cli_path` 选项，直接传 Claude CLI 路径，不用再打 `SubprocessCLITransport._find_cli` 补丁

相应改动在提交 `b493b01`。

### 2. 限流三分类 + 重试可观测性（2026-04-19）

上游只针对瞬时网络错做单次重试，对服务端限流和套餐配额不做区分。本 fork 把错误分成三类并对用户可见：

| 类型 | 触发信号 | 行为 |
|---|---|---|
| **A. 套餐额度耗尽** | `RateLimitEvent` status=`rejected`，type ∈ `five_hour`/`seven_day*`/`overage` | 不 retry，Telegram 显示 `📊 Claude 套餐配额已达上限`，告知窗口类型和恢复时间。窗口内再有错误直接跳过重试 |
| **B. 服务端临时压力** | Exception 含 `overloaded`/`429`/`529`/`rate limit`/`too many requests`/`503` | 重连一次，Telegram 显示 `⚠️ 服务端临时限流，正在自动重试（1/1）…` |
| **C. 网络/子进程错** | `TimeoutError`/`ConnectionError`/`OSError` 等 | 重连一次，Telegram 显示 `⚠️ 连接中断，正在重建连接重试（1/1）…` |
| **P. 永久错误** | `AttributeError`/`ValueError`/`Permission denied` 等 | 不 retry，Telegram 显示 `❌ <type>: <msg>`，注明为永久性错误 |

配合 `allowed_warning` 事件：配额用到 ≥ 阈值时 Telegram 会提前显示 `⚠️ Claude 配额使用 85% — 接近限流阈值`。

相应改动在提交 `13948a8`。

### 3. `.env` 加载纪律

`utils/config.py` 按优先级加载：
1. `PROJECT_ROOT/.telegram_bot/.env`（项目级，**高优先级**）
2. `~/telegram_bot/.env`（安装级，fallback）

**纪律**：`TELEGRAM_BOT_TOKEN` 只在 `~/telegram_bot/.env` 里设置，让多项目共用同一个 bot 安装；**项目级 `.env` 不要设置 `TELEGRAM_BOT_TOKEN`**，否则一旦错写成 placeholder 会覆盖 fallback 导致 `InvalidToken` 崩溃。见 `docs/TROUBLESHOOTING.md` 的"env token fallback 被污染"条目。

启动成功时 start.sh 会打印 `ℹ️ Using TELEGRAM_BOT_TOKEN from ~/telegram_bot/.env (fallback)`，这是 fallback 生效的正信号。

### 4. 照片/文档附件转发（继承自 f214d73）

Telegram 发图片或附件给 bot，会下载到 `PROJECT_ROOT/.telegram_bot/uploads/<user_id>/<timestamp>_<filename>`，然后把绝对路径（带 caption）作为文本发给 Claude。50 MB 上限，文件名会做 sanitize。

## 安装推荐

新机器推荐走 upstream 的标准安装，但**克隆本 fork**：

```bash
git clone https://github.com/iamforrest/claude-telegram-bot-bridge.git ~/telegram_bot
cd ~/telegram_bot
./setup.sh       # 或：运行 Claude Code 后发 /setup
```

安装后的日常运维见 `docs/OPERATIONS.md`，跨机器修 bug 与同步见 `docs/DEVELOPMENT.md`，遇到问题先查 `docs/TROUBLESHOOTING.md`。

## 和上游的关系

- 定期从上游 merge 功能更新（酌情）
- 本 fork 的改动（SDK 迁移、三分类 retry）理论上上游也该做；若上游迁移完成且更稳定，未来可以考虑收束回上游
- 本 fork 不追求成为通用发行版，更接近 "作者自用 + 对配置有同类需求的人复用"

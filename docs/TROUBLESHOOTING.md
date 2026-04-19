# 故障排查

按症状查。找不到就打开日志 `<PROJECT_ROOT>/.telegram_bot/logs/bot.log` 和 `supervisor.log` 看最后几十行。

## 症状速查表

| 症状 | 原因 | 解决 |
|---|---|---|
| `No module named telegram_bot` | clone 目录不叫 `telegram_bot` | 重命名目录为 `telegram_bot`，或建软链 |
| `ValueError: Unknown scheme for proxy URL URL('')` | `.env` 里 `PROXY_URL` 是空字符串 | 要么给完整 URL（如 `http://127.0.0.1:1082`），要么整行删掉 |
| Telegram 红叉 `Unknown message type: rate_limit_event` | SDK 不认识新事件（只在上游 terranc + `claude-code-sdk 0.0.25` 发生） | 切到本 fork（已迁移到 `claude-agent-sdk`，见 `docs/FORK_NOTES.md`） |
| `TypeError: unsupported operand type(s) for \|: 'type' and 'NoneType'`（只在 `--status` 出现） | start.sh 的 status check 用了 PEP 604 `X \| None` 语法，但系统默认 python3 是 3.9 | 用 `ps aux \| grep "python -m telegram_bot"` 代替；或升 python3 到 3.10+ |
| bot 起了但 Telegram 没响应 | 代理挂了 / token 错了 / 被 Telegram 踢出 | 跑下方"诊断命令"的代理和 token 检查 |
| `InvalidToken` 疯狂重启，supervisor 5 次放弃 | 项目级 `.env` 里有坏的 `TELEGRAM_BOT_TOKEN` 覆盖了 runtime fallback | 见下方 "env token fallback 被污染" |
| `getMe` 返回 404 | Bot token 已被 BotFather `/revoke` | 重新生成 token，写入 `~/telegram_bot/.env`，重启 |
| `Another bot instance is already running` (Conflict) | 同一 token 被两个进程 long-poll | 先 `./start.sh --path <ROOT> --stop`，然后 `ps aux \| grep python` 杀掉漏网进程，再启动 |
| 日志里反复出现 `Attempted to exit cancel scope in a different task` | SDK 超时后断连接的 anyio 已知问题，一次性错误 | 单独看是噪音；如果 bot 整体挂了同时看有没有 `This HTTPXRequest is not initialized!` 一起出现 |
| bot 回复里收到 `📊 Claude 套餐配额已达上限` | 你的 Claude Max/Pro 套餐窗口用完了 | 等通知中的恢复时间，或切模型（`/model`） |
| bot 回复里收到 `⚠️ 服务端临时限流` 后正常恢复 | Anthropic API 服务端瞬时过载，本 fork 自动重试了一次 | 正常现象，无需处理 |

## env token fallback 被污染（高频坑）

**症状**：启动后立刻 `❌ Invalid Telegram Bot Token`，supervisor 重启 5 次后放弃。

**背景**：`utils/config.py` 加载顺序是 `<PROJECT_ROOT>/.telegram_bot/.env`（项目级）→ `~/telegram_bot/.env`（安装级）。项目级优先。

**触发方式**：某个 setup 流程把 `<PROJECT_ROOT>/.telegram_bot/.env` 写成了 `.env.example` 的原始模板，里面的 placeholder `TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz` 比真 token 优先级高，Bot 初始化时请求 `getMe` 被 Telegram 用 `InvalidToken` 拒绝。

**修复**：

```bash
# 备份然后注释掉项目级 .env 里的 TELEGRAM_BOT_TOKEN 行
cp <PROJECT_ROOT>/.telegram_bot/.env <PROJECT_ROOT>/.telegram_bot/.env.bak
sed -i '' 's/^TELEGRAM_BOT_TOKEN=/# TELEGRAM_BOT_TOKEN=/' <PROJECT_ROOT>/.telegram_bot/.env

# 重启
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --stop
./start.sh --path <PROJECT_ROOT> -d
```

**确认修复生效**：看 start.sh 输出里有

```
ℹ️ Using TELEGRAM_BOT_TOKEN from ~/telegram_bot/.env (fallback)
```

这一行 = fallback 路径走通。

**预防**：永远只在 `~/telegram_bot/.env` 里放 `TELEGRAM_BOT_TOKEN`。项目级 `.env` 只用来存项目相关 override（`CLAUDE_PROCESS_TIMEOUT`、`DRAFT_UPDATE_*` 等），不要碰 token。

## 诊断命令

**1. bot 进程活没活**

```bash
ps aux | grep "python -m telegram_bot" | grep -v grep
```

**2. bot 自报健康状态**

```bash
cat <PROJECT_ROOT>/.telegram_bot/health.json | python3 -m json.tool
```

关注 `service.state`（应为 `available`）、`telegram.state`、`claude.state`。

**3. 代理还在听吗**

```bash
PROXY_URL=$(grep -E "^PROXY_URL=" ~/telegram_bot/.env | cut -d= -f2-)
PROXY_HOST=$(echo "$PROXY_URL" | sed -E 's#http://([^:/]+).*#\1#')
PROXY_PORT=$(echo "$PROXY_URL" | sed -E 's#.*:([0-9]+).*#\1#')

nc -z -w 1 "$PROXY_HOST" "$PROXY_PORT" && echo "proxy up" || echo "proxy DOWN"
```

**4. 代理能通 Telegram 吗**

```bash
curl -s -x "$PROXY_URL" -m 5 https://api.telegram.org/ \
  -o /dev/null -w "HTTP %{http_code}\n"
```

看到 `HTTP 302` = 通；`HTTP 000` 或 curl 报错 = 代理挂了。

**5. Bot token 有效吗**

```bash
TOKEN=$(grep -E "^TELEGRAM_BOT_TOKEN=" ~/telegram_bot/.env | cut -d= -f2-)
curl -s -x "$PROXY_URL" "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -m json.tool
```

`"ok": true` 并看到 bot username = token 有效。`401 Unauthorized` = token 被 revoke。

**6. Claude CLI 能跑吗**

```bash
claude --version
which claude
echo '{"role":"user","content":"ping"}' | claude --print 2>&1 | head -5
```

如果 `claude` 找不到，设 `~/telegram_bot/.env` 里的 `CLAUDE_CLI_PATH=/absolute/path/to/claude`。

## 历史坑（了解即可，现已不需要处理）

### `rate_limit_event` 未知消息（已被 fork 迁移规避）

历史上 Claude CLI v2.1.x 发 `rate_limit_event` 消息，老 `claude-code-sdk 0.0.25` 不认识，抛 `MessageParseError: Unknown message type: rate_limit_event`。曾经靠打 venv 里 `claude_code_sdk/_internal/message_parser.py` 的 monkey patch 把未知消息降级成 SystemMessage 静默忽略。

**现在不需要**：本 fork 已迁移到 `claude-agent-sdk`，原生支持 `RateLimitEvent`，并且 bridge 层把 rate limit 的三类场景做了分类处理（A/B/C，见 `docs/FORK_NOTES.md`）。如果你 clone 的是上游 terranc（还用老 SDK），要么切到本 fork，要么手动迁移（参见 `core/project_chat.py` 的 `claude-agent-sdk` 导入和 `system_prompt` 配置）。

## 还是搞不定

1. `./start.sh --path <ROOT> --debug` 在前台跑，复现后看完整日志
2. 把 `bot.log` 和 `supervisor.log` 最后 200 行 + `health.json` 贴出来
3. 如果是代码改动导致的：`git log --oneline -10` 看最近哪几个 commit 引入

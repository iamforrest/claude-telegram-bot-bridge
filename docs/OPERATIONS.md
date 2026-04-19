# 日常运维

装完 bot（见 `README.md` / `README-zh.md` 的 Quick Start 或运行 `./setup.sh`）之后，日常用的命令都在这里。

## 启停 / 状态

```bash
# 启动（daemon / 后台）
cd ~/telegram_bot && ./start.sh --path <PROJECT_ROOT> -d

# 启动（前台，调试用，Ctrl+C 退出）
cd ~/telegram_bot && ./start.sh --path <PROJECT_ROOT>

# 停止
cd ~/telegram_bot && ./start.sh --path <PROJECT_ROOT> --stop

# 调试模式（更多日志、聊天日志文件）
cd ~/telegram_bot && ./start.sh --path <PROJECT_ROOT> --debug -d
```

`<PROJECT_ROOT>` 是你想让这个 bot 管的目录。建议 `~/workspace`（或所有 repo 的公共父目录），让 Claude 在所有子仓库间自由穿梭，不用频繁切项目。

**查状态**：优先用 start.sh 自带的命令：

```bash
./start.sh --path <PROJECT_ROOT> --status
```

> ⚠️ 已知问题：如果系统默认 `python3` 是 3.9（pyenv 或旧 macOS 自带），`--status` 可能报 `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`（PEP 604 语法）。bot 本身不受影响。详见 `docs/TROUBLESHOOTING.md`。

fallback 方式：

```bash
ps aux | grep "python -m telegram_bot" | grep -v grep
```

看到 `python -m telegram_bot` 进程 = 活着。

## 日志

所有 runtime 产出都在 `<PROJECT_ROOT>/.telegram_bot/` 下：

```bash
# 主日志
tail -f <PROJECT_ROOT>/.telegram_bot/logs/bot.log

# supervisor 启停/重启日志
tail -f <PROJECT_ROOT>/.telegram_bot/logs/supervisor.log

# 当天错误日志
tail -f <PROJECT_ROOT>/.telegram_bot/logs/error_$(date +%Y-%m-%d).log

# 崩溃记录
ls -lt <PROJECT_ROOT>/.telegram_bot/logs/crash_*.log | head -5

# 健康状态（JSON）
cat <PROJECT_ROOT>/.telegram_bot/health.json
```

换 `<PROJECT_ROOT>` 日志路径也跟着换位置。旧日志 14 天自动清理。

## 升级

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --upgrade
```

这条命令内部会：`git pull → venv/bin/pip install -r requirements.txt → restart`。是跨机器同步新改动的标准入口。

手动三步走也可以（等效）：

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --stop
git pull
venv/bin/pip install -r requirements.txt
./start.sh --path <PROJECT_ROOT> -d
```

想知道跨机器开发怎么在一台 Mac 改完推给其他 Mac，见 `docs/DEVELOPMENT.md`。

## Telegram 端常用命令

直接在 bot 聊天窗口发：

| 命令 | 作用 |
|---|---|
| `/start` | 新会话 |
| `/new` | 重开当前会话 |
| `/model` | 切 Sonnet / Opus / Haiku |
| `/resume` | 浏览历史会话，选一个继续 |
| `/history` | 查看当前会话最近消息（分页按钮） |
| `/stop` | 中断当前正在跑的任务（优先级，绕过队列限制） |
| `/revert` | 回滚到某条历史消息（5 种模式：代码+对话/仅对话/仅代码/从此处总结/取消） |
| `/skills` | 列可用 skills |
| `/skill <name>` | 执行某个 skill |
| `/command <cmd>` | 跑一个 Claude Code 斜杠命令 |

## 换工作目录（切项目）

`PROJECT_ROOT` 由 `--path` 在启动时定死，不支持运行时切换。换目录必须重启：

```bash
cd ~/telegram_bot
./start.sh --path <OLD_ROOT> --stop
./start.sh --path <NEW_ROOT> -d
```

**推荐做法**：`--path` 直接指所有 repo 的公共父目录（如 `~/workspace`），让 Claude 在所有子仓库间自由穿梭，就不用切。每次切根还要停一次 bot，会打断对话。

## 多 bot 并行（不同项目独立会话）

想同时在 `projA` 和 `projB` 两个项目里各有独立会话？起两个 bot：

1. **BotFather 再 `/newbot` 建第二个 bot**，拿第二个 token
2. **Clone 第二份代码** 到一个新目录（例如 `~/telegram_bot_b`）——必须以 `telegram_bot` 结尾或作为软链名，因为 `start.sh` 用 `python -m telegram_bot`：

   ```bash
   git clone https://github.com/iamforrest/claude-telegram-bot-bridge.git ~/telegram_bot_b_src
   ln -s ~/telegram_bot_b_src ~/telegram_bot_b  # 或直接 clone 成 ~/telegram_bot_b
   ```

3. **第二份 `.env`** 里写第二个 token 和（可选）独立的 `ALLOWED_USER_IDS`
4. **启动第二个 bot**，指向另一个 PROJECT_ROOT：

   ```bash
   cd ~/telegram_bot_b && ./start.sh --path ~/workspace/projB -d
   ```

5. 每个 bot 对应 Telegram 里一个独立联系人窗口

**注意**：Claude 账号配额是全账号共享的，多 bot 并行跑 = 更快耗 Pro/Max 配额。

## 开机自启（launchd，仅 macOS）

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --install
```

会生成 `~/Library/LaunchAgents/` 下的 plist，重启 Mac 后 bot 自动拉起。取消：

```bash
./start.sh --path <PROJECT_ROOT> --uninstall
```

如果还没装 launchd，bot 崩溃时 supervisor 自动重启（60s 内连续崩 5 次放弃）；但 Mac 重启之后不会自己起来，需要手动 `-d` 一次。建议装 launchd。

## 换 Bot Token（revoke 后）

1. BotFather 里对该 bot 发 `/revoke` 拿新 token
2. 编辑 `~/telegram_bot/.env` 改 `TELEGRAM_BOT_TOKEN`
3. 重启 bot：`./start.sh --path <ROOT> --stop && ./start.sh --path <ROOT> -d`

## 代理故障排查

大陆用户常见问题：Telegram 这边消息发不出去、bot 不回。查：

```bash
# 代理端口还在听吗
nc -z -w 1 127.0.0.1 <PROXY_PORT> && echo "up" || echo "DOWN"

# 代理能通 Telegram 吗
curl -s -x http://127.0.0.1:<PROXY_PORT> -m 5 https://api.telegram.org/ \
  -o /dev/null -w "HTTP %{http_code}\n"
```

看到 `HTTP 302` = 代理通。看到 `HTTP 000` 或 curl 超时 = 代理挂了或端口变了。

如果代理端口变了，改 `~/telegram_bot/.env` 的 `PROXY_URL` 后重启 bot。

## 卸载

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --stop
./start.sh --path <PROJECT_ROOT> --uninstall  # 如果装过 launchd
rm -rf ~/telegram_bot
rm -rf <PROJECT_ROOT>/.telegram_bot           # 日志和 session 数据

# 可选：在 BotFather 里 /deletebot 删掉 Telegram 侧 bot
```

## 安全检查清单

跑 bot 前确认：

- [ ] `~/telegram_bot/.env` 的 `chmod` 是 `600`（只你能读）
- [ ] `.env` 在 `.gitignore` 里（upstream 已配置），没进 Git
- [ ] `ALLOWED_USER_IDS` 只有你自己的 user ID（可多个，逗号分隔）
- [ ] Bot Token 从没贴到公开聊天/截图/Git commit
- [ ] `PROJECT_ROOT` 不是 `~` 这种涵盖 `~/.ssh` / `~/.aws` / `~/.config` 的大目录
- [ ] 没用 `--dangerously-skip-permissions`（不需要，`PROJECT_ROOT` 沙箱已经够了）
- [ ] 项目级 `.env`（`<PROJECT_ROOT>/.telegram_bot/.env`）**没有**设置 `TELEGRAM_BOT_TOKEN`（见 `docs/FORK_NOTES.md` "env 加载纪律"，误设 placeholder 会让 bot 启动失败）

如果 token 可能泄露，在 @BotFather 里对这个 bot 发 `/revoke` 换新 token，更新 `.env`，重启 bot。

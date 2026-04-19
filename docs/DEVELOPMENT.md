# 跨机器开发与同步

这份文档讲：**在任意一台 Mac 上改本 fork 的代码，如何让所有机器上的 bot 都拿到新改动**。

## 仓库角色

```
GitHub: iamforrest/claude-telegram-bot-bridge     ← 唯一真相源（origin）
         ↑ push                    ↓ pull
┌────────┴────────┐        ┌───────┴──────────┐
│ 改代码的 Mac A  │        │ 只跑 bot 的 Mac B │
│ ~/telegram_bot  │        │ ~/telegram_bot    │
└─────────────────┘        └───────────────────┘
```

- **远端 origin**（GitHub 上的 fork）是唯一真相源
- 每台 Mac 本地的 `~/telegram_bot` 都是一个 git checkout
- 改动只能通过 "某台机器 push → 其它机器 pull" 流动

## 两种本地模式

### 模式 A：单 checkout（推荐，适合大多数机器）

只有 `~/telegram_bot` 一份代码，**既是开发目录也是 runtime**。最简单。

```
~/telegram_bot/              ← 改代码 + commit + push + 跑 daemon，都在这
```

### 模式 B：双 checkout（当前主 Mac 的用法）

源码放在 workspace 下做开发，runtime 独立一份：

```
~/Documents/workspace/claude-telegram-bot-bridge/   ← 改代码 + commit + push
~/telegram_bot/                                     ← pull 后启动 daemon
```

**为什么会有这个模式**：作者主 Mac 用 `~/workspace` 作为所有项目的公共父目录，bot 的源码在那下面能和其他仓库一起被 IDE 管理；但 `python -m telegram_bot` 需要模块目录名叫 `telegram_bot`，所以 daemon 跑在 `~/telegram_bot` 的独立 checkout。

只有这种场景才用双 checkout。新机器上直接用模式 A。

## 修 bug 标准流程

假设你在 Mac A 发现了一个 bug，想修完后让所有机器生效。

### 1. 在开发目录里改代码 + 跑测试

**模式 A**：

```bash
cd ~/telegram_bot
# 改代码
venv/bin/python -m unittest discover -s tests -v 2>&1 | tail -20
```

**模式 B**：

```bash
cd ~/Documents/workspace/claude-telegram-bot-bridge
# 改代码
# 测试需要让 python 能 import "telegram_bot" 包，用临时符号链接：
mkdir -p /tmp/bridge_test
ln -sfn "$PWD" /tmp/bridge_test/telegram_bot
cd /tmp/bridge_test && PROJECT_ROOT=~/workspace PYTHONPATH=/tmp/bridge_test \
  ~/telegram_bot/venv/bin/python -m unittest discover \
  -s /tmp/bridge_test/telegram_bot/tests 2>&1 | tail -20
```

### 2. Commit + push

```bash
git add <files>
git commit -m "fix: xxx"
git push origin master
```

### 3. 让所有跑着 bot 的机器拉取新版

每台机器上：

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --upgrade
```

`--upgrade` 一键做完 `stop → git pull → pip install -r requirements.txt → start`。

### 4.（仅模式 B）Mac A 自己的 runtime 也要 pull

模式 A 的 Mac 已经在开发目录里 push 了，但**模式 B 的本机 runtime 还在旧 commit**——需要手动 pull：

```bash
cd ~/telegram_bot
./start.sh --path <PROJECT_ROOT> --upgrade
```

## 纪律（踩过坑的教训）

### ❌ 不要直接改 runtime 目录

**反例**：在 `~/telegram_bot/core/bot.py` 里直接改、不 commit、就重启。

**为什么不行**：
- 下次 `git pull` 拉到 upstream 或 origin 的新改动时，你的本地改会冲突
- 其他机器永远拿不到这个改动
- 以后别人（包括未来的你）会不知道为什么这台机器行为和别人不一样

**遇到过的真实例子**：曾经 runtime 的 `core/bot.py` 被直接改了 112 行（加了 photo/document 附件功能），后来这份改动被手工复制到源码仓 commit 成 `f214d73`，但 runtime 那份没 `reset`，结果切换 git remote 时 pull 失败，要多一步 `git checkout -- core/bot.py` 清掉本地改才行。

**正确做法**：
- 模式 A：直接在 `~/telegram_bot` 改并 commit，`push` 之后 `--upgrade` 重启
- 模式 B：改 `~/Documents/workspace/claude-telegram-bot-bridge`，push 之后 `~/telegram_bot` 走 `--upgrade`

### ❌ 不要在项目级 `.env` 里设 `TELEGRAM_BOT_TOKEN`

`utils/config.py` 加载顺序是：
1. `<PROJECT_ROOT>/.telegram_bot/.env`（项目级，高优先级）
2. `~/telegram_bot/.env`（安装级，fallback）

token 只应该在 `~/telegram_bot/.env`。项目级 `.env` 用来放项目相关的 overrides（比如 `CLAUDE_PROCESS_TIMEOUT`、`DRAFT_UPDATE_MIN_CHARS`），**不设 token**。

**为什么强调**：曾经项目级 `.env` 被某个 setup 流程覆盖成 `.env.example` 原模板，里面的 placeholder `TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz` 覆盖了真 token，bot 反复 `InvalidToken` 崩溃 5 次后 supervisor 放弃。见 `docs/TROUBLESHOOTING.md` 的 "env token fallback 被污染"。

启动时 start.sh 如果打印 `ℹ️ Using TELEGRAM_BOT_TOKEN from ~/telegram_bot/.env (fallback)`，说明 fallback 生效；**没有这行提示**（且项目级 `.env` 有 `TELEGRAM_BOT_TOKEN=...`）就要检查有没有被污染。

### ❌ 不要用 `--no-verify` 跳过 pre-commit

Pre-commit hook 有失败就排查根因，不要 bypass。

### ✅ 跨机器想知道"其它机器都是什么版本了"

每台机器上跑：

```bash
cd ~/telegram_bot && git log --oneline -3
```

三台机器都该指向同一个 commit；如果某台落后，对这台跑 `--upgrade`。

## 新机器第一次搭建（简要）

详情见 upstream README 的 Quick Start，这里只列跨机器专用步骤：

```bash
# 1. Clone 本 fork（不是 terranc 上游）到 ~/telegram_bot（目录名重要）
git clone https://github.com/iamforrest/claude-telegram-bot-bridge.git ~/telegram_bot

# 2. 标准安装
cd ~/telegram_bot
./setup.sh     # 交互式，会问 token、ALLOWED_USER_IDS、代理

# 3. 启动
./start.sh --path <PROJECT_ROOT> -d

# 4.（可选）装 launchd 开机自启
./start.sh --path <PROJECT_ROOT> --install
```

完成后这台机器就加入了 "改完 push → 所有机器 `--upgrade`" 的循环。

## 依赖变了怎么办

如果改动涉及 `requirements.txt`（加包、升版本）：

- `./start.sh --upgrade` 内部会自动检测 `requirements.txt` 的 MD5 变化并重装，**无需手动操作**
- 如果手动 `git pull` 后再 `-d`（而不是 `--upgrade`），start.sh 同样会检测 hash 变化跑 pip install

只有**删包**（如 `pip uninstall claude-code-sdk`）时需要手动干预，因为 `pip install -r requirements.txt` 不会卸载文件里没有的旧包。参考 2026-04-19 从 `claude-code-sdk` 切到 `claude-agent-sdk` 的流程。

## 上游 terranc 也有新改动怎么办

如果想合并上游新 feature 到本 fork：

```bash
cd ~/Documents/workspace/claude-telegram-bot-bridge  # 或 ~/telegram_bot

# 一次性添加 upstream remote
git remote add upstream https://github.com/terranc/claude-telegram-bot-bridge.git

# 拉取 + 合并
git fetch upstream
git merge upstream/master    # 或 rebase，看偏好

# 解决冲突（SDK 迁移相关文件大概率有冲突），跑测试，push
git push origin master
```

其他机器跟着 `--upgrade` 就行。

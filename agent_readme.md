# agent_readme.md — Feishu Bridge 环境自适应指南

> **读者**：AI coding agent（opencode / claude code / cursor / ZCode 等）。
> **目的**：让 agent 在一台陌生机器上，**自动判定** feishu-bridge 的运行环境，
> 按**自愈剧本**修复缺失项，最终让它可用。读这一份就够，不需要先读 README.md。

---

## 0. 这是什么（架构概览）

feishu-bridge 是一个「飞书消息 → AI 编码 agent」的桥接器。三条进程，各司其职：

```
手机飞书 App
   │  (文本指令，WebSocket 长连接，无需公网 IP)
   ▼
┌─────────────────────────────────────────────────────────────┐
│  bridge.py  ← 跑在【宿主机】(Windows/macOS/Linux/WSL 均可)    │
│    · 飞书 WS 长连接 + 白名单校验(只认 ALLOWED_USER_ID)        │
│    · subprocess 调用 `docker exec` 把指令送进容器            │
│    · 接收 agent 的 JSON-lines 输出，渲染成飞书卡片            │
│    · 写类工具触发确认卡片，用户点 ✅/🚫 才放行                │
└─────────────────────────────────────────────────────────────┘
   │  docker exec -i feishu-claude-agent python3 /app/...
   ▼
┌─────────────────────────────────────────────────────────────┐
│  Docker 容器 feishu-claude-agent  ← 隔离的执行环境           │
│    · 只挂载 WORKSPACE_DIR（破坏面锁死于此）                  │
│    ├── agent_runner.py          → Claude Agent SDK 引擎      │
│    └── agent_runner_opencode.py → OpenCode serve + SSE 引擎  │
└─────────────────────────────────────────────────────────────┘
   │  改 WORKSPACE_DIR 里的文件
   ▼
用户真实代码目录
```

**两个关键设计决策（改代码前必须理解）：**

1. **bridge.py 必须在宿主机，不能容器化。**
   - 它要 `docker exec` 调度 agent 容器。若把 bridge 也塞进容器，要么挂
     `docker.sock`（= 宿主机 root，破坏安全边界），要么 DinD（agent 碰不到
     宿主机真实代码目录，产品失效）。bridge 留宿主机是**安全特性**。

2. **agent 必须在容器里。**
   - 飞书消息来自外部，agent 执行任意代码/命令。容器把它关在 WORKSPACE_DIR
     一个目录内，其余宿主机文件碰不到。这是核心安全档位。

**因此，环境适配的目标是：让 bridge.py 能在任何宿主机 OS 上用 `python3`
直接跑起来**，不依赖 systemd / WSL / bash 专属工具。这已经通过 `cli.py`
和 bridge.py 的跨平台改造实现。

---

## 1. 环境判定清单（逐项 CHECK）

**一键自检**：运行 `python3 cli.py check`。它会输出每项 ✓/✗。
任何 ✗ 都跳到 §2 对应的自愈节。下面是每项的检测逻辑（agent 可单独执行复核）。

> 所有命令的 `python3` 在 Windows 上可替换为 `python`。`cli.py` 位于
> feishu-bridge 项目根目录。

### 1.1 操作系统识别

```bash
# 检测
python3 -c "import platform; print(platform.system(), platform.release())"
# 期望: Linux / Darwin / Windows 之一。Windows 上若需 WSL，另查 `wsl -l -v`
```
- **Linux**：原生最简，docker engine + python3 即可。
- **Darwin (macOS)**：装 Docker Desktop + python3。
- **Windows**：装 Docker Desktop（WSL2 backend）+ python3。bridge.py 跑在
  Windows 原生 python（无需进 WSL），docker 命令通过 PATH 找到 Docker Desktop。
- **WSL 内**：等价 Linux，但 Docker 通过 Docker Desktop 的 socket 注入。

### 1.2 Python ≥ 3.10

```bash
python3 --version          # 期望: Python 3.10+
python3 -c "import sys; assert sys.version_info >= (3,10)" && echo OK
```
→ 不满足见 §2.1

### 1.3 Docker daemon 可用

```bash
docker info --format '{{.ServerVersion}}'    # 期望: 打印版本号
```
→ 不满足见 §2.2

### 1.4 Docker compose 可用

```bash
docker compose version --short    # 期望: 打印版本 (v2 插件)
# 若失败，回退检查:
docker-compose version --short    # 旧版独立二进制
```
→ 不满足见 §2.2（compose 随 Docker Desktop 自带）

### 1.5 凭证文件存在且字段齐全

cli.py 按这个顺序找 env 文件：`$FEISHU_ENV_FILE` → `./.env` →
`~/.secrets/feishu-bridge.env`。

```bash
# 检测文件存在
python3 cli.py check    # "Credentials (.env)" 行
# 或手动定位
ls -la "${FEISHU_ENV_FILE:-$HOME/.secrets/feishu-bridge.env}"
```
必填字段（值不能为空、不能是 `xxx...` 占位）：
- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`ALLOWED_USER_ID`
→ 不满足见 §2.3

### 1.6 WORKSPACE_DIR 存在且可写

```bash
# WORKSPACE_DIR 来自 env 文件或环境变量；默认 ~/projects
python3 -c "import os; d=os.environ.get('WORKSPACE_DIR') or os.path.join(os.path.expanduser('~'),'projects'); print(d); assert os.path.isdir(d) and os.access(d,os.W_OK), 'missing/unwritable'"
```
→ 不满足见 §2.4

### 1.7 agent 容器运行中

```bash
docker inspect -f '{{.State.Status}}' feishu-claude-agent    # 期望: running
```
→ 不是 running/不存在 见 §2.5

### 1.8 bridge 进程运行中

```bash
python3 cli.py status    # 看 "bridge.py: RUNNING / NOT RUNNING"
```
bridge 的存活判定（cli.py 内部逻辑，无需 ps/pgrep）：
- 先探 advisory lock（新 bridge.py 持有的文件锁）
- 再回退查 PID 文件里的 PID 是否存活（兼容旧版/systemd 启动的实例）
→ 不在跑 见 §2.6

### 1.9 OpenCode auth（仅 ENGINE=opencode 时需要）

```bash
# 检测默认位置
ls -la ~/.local/share/opencode/auth.json
# 或 env 文件里 OPENCODE_AUTH_FILE 指定的路径
```
- 若 `ENGINE=claude`（默认），此项跳过。
→ 缺失见 §2.7

---

## 2. 自愈剧本（FIX）

**通用入口**：`python3 cli.py doctor` 会跑全部检查并打印每项失败的修复命令。
agent 可解析它的 `FIX →` 行直接执行。下面是详细剧本。

**最小启动顺序（从零到可用，按序执行；每步失败跳到对应小节）：**
```
1. python3 cli.py check                 # 看缺什么 → §2.1/§2.2
2. cp .env.example ~/.secrets/feishu-bridge.env  &&  编辑填真实值
   （必填 FEISHU_APP_ID / FEISHU_APP_SECRET / ALLOWED_USER_ID / WORKSPACE_DIR；
     Claude 引擎还要 ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN）→ §2.3/§2.4
3. export FEISHU_ENV_FILE="$HOME/.secrets/feishu-bridge.env"   # 必做！见 §2.3
   （opencode 引擎另需 export OPENCODE_AUTH_FILE=绝对路径 → §2.7）
4. python3 cli.py up                    # 建容器（首次 5-10 分钟）→ §2.5
5. python3 cli.py start --detach        # 启 bridge → §2.6
6. python3 cli.py check                 # 期望全 ✓
7. 飞书发一条消息做端到端验证 → §4
```
> 第 3 步的 export 只对当前 shell 有效；长期运行请写进 shell profile 或
> systemd unit（见 §2.3 末尾），否则重启后 bridge 又会 Missing .env。

### 2.1 Python 缺失 / 版本过低

| OS | 安装命令 |
|----|---------|
| Linux (Debian/Ubuntu) | `sudo apt update && sudo apt install -y python3 python3-venv python3-pip` |
| Linux (Fedora/RHEL) | `sudo dnf install -y python3 python3-pip` |
| macOS | `brew install python@3.12` 或装 https://www.python.org/downloads/ |
| Windows | 下载 https://www.python.org/downloads/ （勾选 "Add to PATH"） |
| WSL 内 | 同 Linux |

建议建 venv（仅宿主机依赖 `lark-oapi`，很轻）：
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # Windows: .venv\Scripts\pip install -r requirements.txt
```

### 2.2 Docker / compose 缺失

- **Windows / macOS**：装 **Docker Desktop**（自带 compose v2、WSL2 backend）。
  https://www.docker.com/products/docker-desktop/ 。装完启动 Docker Desktop，
  等托盘图标变绿（docker engine ready）。
- **Linux**：
  ```bash
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker $USER        # 免 sudo，需重新登录生效
  sudo apt install -y docker-compose-plugin
  ```
- **WSL 内**：通常用 Docker Desktop 的 WSL 集成（Settings → Resources →
  勾选发行版），无需在 WSL 里单独装 docker。
- **验证**：`docker info` 成功即就绪。

> Docker Desktop 启动后 socket 可能延迟 10-30s 才可用。若 `docker info`
> 反复失败，等一会再试。

### 2.3 凭证缺失 / 字段不全

凭证放在 `~/.secrets/feishu-bridge.env`（**不在仓库内、不在容器挂载区内**，
agent 无法通过 Read 工具读到，这是安全设计）。

```bash
mkdir -p ~/.secrets
cp .env.example ~/.secrets/feishu-bridge.env
chmod 600 ~/.secrets/feishu-bridge.env        # Linux/macOS；Windows 用 ACL
# 然后编辑填入真实凭证
```

必填字段获取方式：
| 字段 | 怎么拿 |
|------|--------|
| `FEISHU_APP_ID` | 飞书开放平台 → 你的自建应用 → 凭证与基础信息 |
| `FEISHU_APP_SECRET` | 同上 |
| `ALLOWED_USER_ID` | 你的飞书 open_id。先随便填一个值启动 bridge，给机器人发条消息，看 `bridge.log` 里 `IGNORED ... sender: ou_xxx`，把 `ou_...` 填回 |

**让 cli.py / bridge.py 找到凭证（必做，不是可选项）：**
```bash
export FEISHU_ENV_FILE="$HOME/.secrets/feishu-bridge.env"
```

⚠️ **`FEISHU_ENV_FILE` 必须显式 export，否则 bridge 会启动失败。** 原因：
cli.py 和 bridge.py 的 env 文件查找逻辑**不一致**——
- `cli.py`（check/doctor）：`$FEISHU_ENV_FILE` → `./.env` →
  `~/.secrets/feishu-bridge.env`（会回退，所以 check 可能全绿）。
- `bridge.py`：**只认** `$FEISHU_ENV_FILE` 和 `./.env`，**不回退** `~/.secrets/`。

所以如果只把凭证放 `~/.secrets/feishu-bridge.env` 而没 export
`FEISHU_ENV_FILE`，会出现「`cli.py check` 全绿但 `cli.py start` 立刻报
`Missing .env`」的诡异现象。**务必 export**，并写进启动 bridge 的 shell
profile 或 systemd unit 的 `Environment=`/`EnvironmentFile=`。

> systemd unit 示例片段：
> ```
> [Service]
> Environment=FEISHU_ENV_FILE=%h/.secrets/feishu-bridge.env
> WorkingDirectory=/path/to/feishu-bridge
> ```

### 2.4 WORKSPACE_DIR 缺失/不可写

`WORKSPACE_DIR` 是 agent 容器能操作的**唯一**目录（你的真实代码目录）。
在 env 文件里设置，例如：
```bash
WORKSPACE_DIR=/home/$USER/projects          # Linux/WSL
# 或 Windows: WORKSPACE_DIR=C:\Users\你的用户名\projects
```
确保目录存在且当前用户可写：`mkdir -p "$WORKSPACE_DIR"`。

⚠️ **`.env.example` 里写死的是占位值 `WORKSPACE_DIR=/home/user/projects`**
（`user` 是占位用户名，不是你的）。`cp` 之后**必须改成真实路径**，否则容器
挂载会失败（挂一个不存在的宿主目录）。同时 `ENGINE=claude`（默认）下别忘填
`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`。

⚠️ 改了 WORKSPACE_DIR 后，**必须重建容器**（§2.5 的 `up`），因为挂载是
compose 启动时确定的。

### 2.5 agent 容器未跑

```bash
python3 cli.py up    # = docker compose -f docker/docker-compose.yml up -d --build
```
cli.py 会自动把 env 文件的变量 export 给 compose（用于 `${WORKSPACE_DIR}` 等
插值）。首次构建会拉镜像、装 Node/opencode，约 5-10 分钟。

验证：
```bash
# 容器入口是 `sleep infinity`（agent 由 bridge.py 在 turn 时调起，不在 PATH 待命），
# 所以不要用 `claude --version` / `opencode --version` 验证——会报 not found 而误判。
# 验证 runner 脚本本身可执行即可：
docker exec feishu-claude-agent python3 /app/agent_runner.py --help          # Claude 引擎
docker exec feishu-claude-agent python3 /app/agent_runner_opencode.py --help # OpenCode 引擎
# 或直接看容器整体健康度:
python3 cli.py check      # "Agent container" 行应为 ✓ running
```

### 2.6 bridge 未跑

```bash
python3 cli.py start --detach    # 后台启动，日志写 bridge.log
# 或前台调试:
python3 cli.py start             # Ctrl-C 停止
```

⚠️ **不要同时跑两个 bridge**。bridge.py 用文件锁保证单实例，第二个会拒绝启动。
若误判卡住：`python3 cli.py stop` 停干净再 start。

### 2.7 OpenCode auth.json 缺失（仅 opencode 引擎）

OpenCode 引擎需要 provider 认证文件，与 API_KEY 不同。在**宿主机**跑一次：
```bash
opencode auth login     # 选 Z.AI Coding Plan（或你的 provider）→ 粘贴 key
```
生成的 `~/.local/share/opencode/auth.json` 会被 compose 挂载进容器（只读）。

⚠️ **挂载路径必须是绝对路径，不能用 `~`**。docker-compose.yml 里写的是
`${OPENCODE_AUTH_FILE:-~/.local/share/opencode/auth.json}`，但 compose **不会
对 `~` 做 shell 展开**——默认值里的 `~` 会被当成字面目录名，挂载到一个怪路径，
容器内 auth 实际读不到。所以必须显式 export 绝对路径：
```bash
export OPENCODE_AUTH_FILE="$HOME/.local/share/opencode/auth.json"
```
（cli.py `up` 时会把这个变量透传给 compose。）若你的 auth 文件在别处，同理
设 `OPENCODE_AUTH_FILE` 为**绝对路径**。设完要 `python3 cli.py down && up`
重建容器（挂载在 compose 启动时确定）。

默认位置验证（宿主机）：
```bash
ls -la "$HOME/.local/share/opencode/auth.json"
```

---

## 3. 配置修改指南（CONFIGURE）

所有配置在 env 文件（默认 `~/.secrets/feishu-bridge.env`）。改完通常需要
`python3 cli.py restart`（bridge）或 `python3 cli.py down && up`（容器）。

### 3.1 换 OpenCode Provider

env 文件改这三个字段：
```bash
OPENCODE_API_KEY=sk-xxx
OPENCODE_API_URL=https://...        # 端点
OPENCODE_MODEL=provider/model       # 见下表
```
| Provider | OPENCODE_API_URL | OPENCODE_MODEL |
|----------|------------------|----------------|
| 智谱 Coding Plan | `https://open.bigmodel.cn/api/coding/paas/v4` | `zhipuai-coding-plan/glm-5.2` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek/deepseek-chat` |
| 任意 OpenAI 兼容 | `https://your-api/v1` | `your-provider/your-model` |

### 3.2 换默认引擎

```bash
ENGINE=claude       # 默认。飞书里 /engine opencode 临时切
ENGINE=opencode     # 默认就用 opencode
```

### 3.3 调整安全档位

```bash
SAFE_TOOLS=Read,Grep,Glob,WebSearch,WebFetch,TodoWrite,NotebookRead   # 自动放行的读类工具
CONFIRM_TIMEOUT=300                                                    # 确认卡片超时秒数（超时自动拒绝）
```
要更严：从 SAFE_TOOLS 删掉 `WebFetch`/`WebSearch`（防 SSRF/数据外泄），
或删掉 `Read`（但会麻烦——agent 读啥都要确认）。

### 3.4 改 WORKSPACE_DIR

env 文件改 `WORKSPACE_DIR`，然后**必须**重建容器（挂载路径变了）：
```bash
python3 cli.py down
python3 cli.py up
```

---

## 4. 验证（VERIFY）

端到端冒烟（全绿即就绪）：
```bash
python3 cli.py check       # 全部 ✓
```
然后**用飞书给机器人发一条消息**，期望：
1. 立刻收到「🤔 已收到,正在处理…」卡片
2. 读类操作自动执行，卡片流式更新结果
3. 写类操作弹「⚠️ 需要确认」卡片，点 ✅ 后执行

引擎切换验证：飞书里发 `/engine opencode` → 再发 `/engine claude`，
每次应收到「⚙️ 引擎已切换」卡片。

确认流程验证：发「在当前目录建一个 hello.txt」，应弹确认卡，点 ✅ 后
`ls` 能看到文件。

---

## 5. 排障决策树（TROUBLESHOOT）

**机器人完全不回消息：**
```
python3 cli.py status
├─ bridge.py NOT RUNNING → python3 cli.py start --detach，看 bridge.log 报什么
├─ bridge 在跑但无反应 → tail bridge.log:
│   ├─ "IGNORED ... sender: ou_xxx" → ALLOWED_USER_ID 没配对，把 ou_xxx 填进 env
│   └─ 反复重连失败 → 网络/DNS 问题，或飞书事件订阅没选「长连接」
└─ 看 §1.5 凭证是否齐全
```

**bridge 启动即退：**
```
python3 cli.py start       # 前台跑，直接看报错
├─ "Missing .env" → 九成是 FEISHU_ENV_FILE 没 export。注意 cli.py 会回退
│   ~/.secrets/ 所以 check 全绿，但 bridge.py 不回退 → 必须 export
│   FEISHU_ENV_FILE="$HOME/.secrets/feishu-bridge.env" 再 start（详见 §2.3）
├─ "Another bridge instance is running" → python3 cli.py stop 再 start
├─ KeyError 'FEISHU_APP_ID' → env 文件缺必填字段 → §2.3
└─ lark-oapi import 失败 → venv 没装依赖 → .venv/bin/pip install -r requirements.txt
```

**容器内 agent 报错（Claude 引擎 ANTHROPIC_* 缺失）：**

⚠️ 先理解凭证注入有**两条路径**，排障必须分清查的是哪一条：
1. **容器启动时**（compose `environment:` 块）——由 cli.py `up` 时把 env 文件
   的 `ANTHROPIC_*` 透传给 compose。这只决定容器**默认**环境。
2. **每次 turn 时**（bridge.py 的 `docker exec -e`）——bridge.py 从**自己进程的
   `os.environ`** 取 `_CLAUDE_FORWARD_VARS` 逐个 `-e` 注入，并且只 `passthrough`
   这些变量给子进程（`_clean_env`）。**这是 agent 运行时实际拿到的那份。**

关键陷阱：bridge.py 不会自己去读 env 文件里的 `ANTHROPIC_*`——它只读
`FEISHU_APP_ID/APP_SECRET/ALLOWED_USER_ID` 三个，其余靠 `os.environ.setdefault`
从 env 文件补。所以 **bridge 进程的 `os.environ` 里有没有 `ANTHROPIC_*`，取决于
启动 bridge 的那个 shell/unit 是否 `export` 了它们**（或 FEISHU_ENV_FILE 指向的
文件里有没有）。

```
docker logs feishu-claude-agent
├─ Claude 报 401/认证失败 → 别只看容器内 env（路径 1 会误导你），
│   真正要查的是「启动 bridge 的环境」:
│   1. 确认 FEISHU_ENV_FILE 指向的文件里 ANTHROPIC_BASE_URL/AUTH_TOKEN 有值且非占位
│   2. 确认启动 bridge 的 shell/systemd unit export 了 FEISHU_ENV_FILE
│      （bridge.py _load_env 只认 $FEISHU_ENV_FILE 和 ./.env，不回退 ~/.secrets）
│   3. 重启 bridge 让它重新加载：python3 cli.py restart
├─ OpenCode: permission denied / auth → §2.7 auth.json
└─ 容器反复重启 → docker inspect 看挂载路径是否存在于宿主机
```

**确认卡点了没反应：**
```
├─ 确认卡显示但点 ✅ 无效 → 操作者 open_id != ALLOWED_USER_ID（卡片回调会校验）
├─ 根本没弹确认卡 → agent 用的是 SAFE_TOOLS 里的工具（自动放行），属正常
└─ bridge.log 有 "confirm card posted" 但飞书没显示 → 飞书卡片回调未到达，查网络
```

**会话错乱/跨会话串：**
```
飞书发 /new 或 /reset → 清当前 chat 的所有引擎会话
```

---

## 6. 关键命令速查

| 操作 | 命令 |
|------|------|
| 环境自检 | `python3 cli.py check` |
| 深度诊断+修复提示 | `python3 cli.py doctor` |
| 启 agent 容器 | `python3 cli.py up` |
| 停 agent 容器 | `python3 cli.py down` |
| 启 bridge(后台) | `python3 cli.py start --detach` |
| 停 bridge | `python3 cli.py stop` |
| 重启 bridge | `python3 cli.py restart` |
| 状态总览 | `python3 cli.py status` |
| 看 bridge 日志 | `python3 cli.py logs -n 100` |
| 跟随 bridge 日志 | `python3 cli.py logs -f` |
| 看容器日志 | `python3 cli.py logs --container` |

所有命令跨平台（Windows/macOS/Linux），纯 Python，不依赖 bash/pgrep/systemd。

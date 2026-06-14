# Feishu ↔ AI Bridge

手机飞书发指令 → WSL2 机器上的 Claude Code / OpenCode 执行 → 结果流式回飞书卡片。
安全档位：**白名单 + 凭证隔离 + Docker 隔离 + 危险操作飞书确认**。

无需公网 IP：用飞书 **WebSocket 长连接**接收事件。
双引擎：**Claude Code**（默认）/ **OpenCode**（飞书 `/engine opencode` 切换）。

---

## 架构

```
手机飞书 App
   │  (文本指令)
   ▼
飞书云 ──WS长连接──▶ bridge.py (宿主机)
                        │  白名单校验 (只认你的 open_id)
                        │  docker exec -i  (注入指令 + 凭证)
                        ▼
                   Docker 容器: feishu-claude-agent
                     ├── agent_runner.py  ←→  Claude Agent SDK
                     └── agent_runner_opencode.py  ←→  OpenCode serve + SSE
                     只挂载 WORKSPACE_DIR (破坏面锁死于此)
                        │
                        │  危险工具 (Bash/Write/Edit/…)
                        │  → can_use_tool / permission.asked 拦截
                        │  → 推确认卡片到飞书
                        ▼
               确认卡片 (✅允许 / 🚫拒绝) → 你点按钮 → 放行/拒绝
```

---

## 前置：飞书开放平台配置（约 5 分钟）

1. 打开 https://open.feishu.cn → **创建企业自建应用**
2. **凭证与基础信息** → 记下 **App ID** 和 **App Secret**
3. **添加应用能力** → 启用**机器人**
4. **权限管理** → 开通：
   - `im:message`（读写单聊消息）
   - `im:message:send_as_bot`（以机器人身份发消息）
5. **事件与回调** → 订阅方式选 **「使用长连接接收事件」**（WebSocket），添加事件：
   - `im.message.receive_v1`（接收消息）
6. 发布应用版本，让自己能给机器人发私聊
7. 拿到你的 **open_id**：启动 bridge 后给机器人发消息，看 `bridge.log` 里
   `IGNORED ... sender: ou_xxx`，把 `ou_...` 填入配置

---

## 配置

### 凭证存放位置（重要）

**所有敏感信息放在 `~/.secrets/feishu-bridge.env`。** 该目录不在容器挂载区内，
agent 无法通过 Read 工具读取。systemd 服务通过 `FEISHU_ENV_FILE` 变量指向它。

```bash
mkdir -p ~/.secrets
cp .env.example ~/.secrets/feishu-bridge.env
chmod 600 ~/.secrets/feishu-bridge.env
# 编辑 ~/.secrets/feishu-bridge.env，填写真实凭证
```

> 如果不用 systemd，也可以直接 `export FEISHU_ENV_FILE=~/.secrets/feishu-bridge.env`
> 然后启动 `python3 bridge.py`。

### 配置项说明

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | 是 | 飞书自建应用的 App ID |
| `FEISHU_APP_SECRET` | 是 | 飞书自建应用的 App Secret |
| `ALLOWED_USER_ID` | 是 | 你的飞书 open_id（唯一白名单） |
| `ANTHROPIC_BASE_URL` | Claude | relayer 端点 |
| `ANTHROPIC_AUTH_TOKEN` | Claude | relayer 认证令牌 |
| `ANTHROPIC_API_KEY` | Claude | 官方 API Key（与 relayer 二选一） |
| `OPENCODE_API_KEY` | OpenCode | 任意 OpenAI 兼容 API 的 Key |
| `OPENCODE_API_URL` | OpenCode | API 端点（默认智谱 Coding Plan） |
| `OPENCODE_MODEL` | 否 | `provider/model`，默认 `zhipuai-coding-plan/glm-5.1` |
| `ENGINE` | 否 | 默认引擎 `claude` 或 `opencode` |
| `WORKSPACE_DIR` | 否 | 挂载进容器的目录 |
| `SAFE_TOOLS` | 否 | 自动放行的读类工具（逗号分隔） |
| `CONFIRM_TIMEOUT` | 否 | 确认卡片超时秒数（默认 300） |

### 换 OpenCode Provider

```bash
# 智谱 Coding Plan（默认）
OPENCODE_API_KEY=xxx
OPENCODE_API_URL=https://open.bigmodel.cn/api/coding/paas/v4
OPENCODE_MODEL=zhipuai-coding-plan/glm-5.1

# DeepSeek
OPENCODE_API_KEY=sk-xxx
OPENCODE_API_URL=https://api.deepseek.com/v1
OPENCODE_MODEL=deepseek/deepseek-chat

# 任意 OpenAI 兼容 API
OPENCODE_API_KEY=sk-xxx
OPENCODE_API_URL=https://your-api.example.com/v1
OPENCODE_MODEL=your-provider/your-model
```

---

## 部署

```bash
cd /path/to/feishu-bridge

# 1) 凭证
mkdir -p ~/.secrets
cp .env.example ~/.secrets/feishu-bridge.env
chmod 600 ~/.secrets/feishu-bridge.env
#    编辑 ~/.secrets/feishu-bridge.env 填入真实凭证

# 2) 宿主机 venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3) 构建并启动隔离容器
export $(grep -v '^#' ~/.secrets/feishu-bridge.env | xargs)
docker compose -f docker/docker-compose.yml up -d --build

#    验证
docker exec feishu-claude-agent claude --version      # Claude
docker exec feishu-claude-agent opencode --version     # OpenCode

# 4) 启动 bridge
systemctl --user daemon-reload
systemctl --user restart feishu-bridge
systemctl --user status feishu-bridge
```

看到 `Bridge up. Whitelisted user: ...` 即就绪。

### systemd 自启

bridge 已注册为 `feishu-bridge.service` 用户服务。

```bash
# 开启 lingering（Windows 注销后服务不回收 —— 只需跑一次）
sudo loginctl enable-linger liu

# 常用命令
systemctl --user status  feishu-bridge      # 看状态
systemctl --user restart feishu-bridge      # 重启
systemctl --user stop    feishu-bridge      # 停止
journalctl --user -u feishu-bridge -f       # 实时日志
```

> WSL + Docker Desktop 特有：Docker 守护进程在 Windows 侧，通过
> `/var/run/docker.sock` 暴露进 WSL。服务启动前 `wait-for-docker.sh`
> 会阻塞等待 docker.sock 就绪。Windows 重启后需开一次 WSL 终端唤醒发行版。

### 手动启动（调试用）

```bash
FEISHU_ENV_FILE=~/.secrets/feishu-bridge.env nohup .venv/bin/python3 bridge.py >> bridge.log 2>&1 &
```

先停 systemd 服务避免双连接：`systemctl --user stop feishu-bridge`

---

## 使用

- 发文本指令，例如：`列出最大的 5 个文件`、`修复 utils.py 的类型错误`
- 读类操作自动执行；写文件 / 跑命令弹**确认卡片**，点 ✅ 执行，超时自动拒绝
- `/new` 或 `/reset`：清除会话上下文，开新会话
- `/engine opencode` 或 `/engine claude`：切换 AI 引擎（每个聊天独立）
- 每个飞书 chat 维护独立会话（`sessions.json` → `chat_id → session_id`）

---

## OpenCode 引擎

### 实现细节

OpenCode 引擎通过 `opencode serve` 的 SSE 事件流 + REST API 驱动：

1. **配置生成**：`_write_opencode_config()` 写入 `~/.config/opencode/opencode.json`，
   设置 `agent.build.permission.edit/bash/external_directory = "ask"`
2. **启动 serve + 建 session**：分配随机端口，`POST /session` 创建会话
3. **SSE 先连后发**：先 `GET /event` 建立 SSE 通道，再 `POST prompt_async`
   （顺序重要 — 否则会错过 `permission.asked` 事件）
4. **事件处理**：
   - `message.part.delta` → 流式文本
   - `message.part.updated` (tool) → `pending` 时记录工具名，`running` 时输出详情（input 仅在 running 态有值）
   - `permission.asked` → 飞书弹确认卡片（非阻塞：SSE 流继续，stdin 线程异步回复）
   - `session.status (idle)` → 结束
5. **camelCase 适配**：OpenCode 工具参数为 `filePath`/`oldString`/`newString`，
   `_normalize_tool_input()` 负责转为 snake_case

### 已知限制

- 由于 `opencode serve` 模式下 `permission.asked` 与工具 `running` 几乎同时触发，
  部分工具的确认卡片是**通知性**的（工具已开始执行），但在 Docker 隔离下风险可控
- zhipuai/glm-5.1 推理耗时较长（简单任务约 4-5 分钟），大部分时间花在 reasoning 阶段

---

## 安全边界

**做到了：**
- 只有 `ALLOWED_USER_ID` 能驱动，其他人忽略并记日志
- 卡片按钮回调校验操作者 open_id，别人点确认卡无效
- 凭证放在 `~/.secrets/feishu-bridge.env`（`chmod 600`），不在仓库内，不在容器挂载区内
- agent 跑在容器内，只挂载 `WORKSPACE_DIR`，碰不到宿主机其他文件
- 写类工具必须飞书确认，超时自动拒绝
- `subprocess.Popen` 只透传必要环境变量，不满屏泄露

**挡不住（剩余风险）：**
- 飞书账号本身被盗 → 对方就是“你”。建议开飞书二次验证
- 你自己批准了破坏性指令 → 确认闸门只在挂载目录内兜底
- 数据流经飞书云 + relayer（`claudeide.net`）
- 容器默认有出网能力（调 AI API）。更严可在 compose 里限制 egress

---

## 文件一览

| 文件 | 作用 |
|------|------|
| `bridge.py` | 宿主机：长连接、白名单、docker exec、卡片渲染、确认回调、引擎切换 |
| `agent_runner.py` | 容器内：Claude Agent SDK + `can_use_tool` 确认 + session resume |
| `agent_runner_opencode.py` | 容器内：OpenCode serve SSE + permission.asked 拦截 + REST API |
| `session_store.py` | chat_id → session_id 映射 |
| `docker/Dockerfile` | 容器镜像：claude CLI + opencode CLI + agent SDK |
| `docker/docker-compose.yml` | 隔离容器，只挂载 workspace |
| `wait-for-docker.sh` | 启动前门禁，等待 Docker Desktop socket 就绪 |
| `.env.example` | 配置模板（复制到 `~/.secrets/feishu-bridge.env`） |

---

## 排障

- **机器人不回**：看 `bridge.log`。常见：`ALLOWED_USER_ID` 没配对，或飞书事件订阅没选「长连接」
- **确认卡点了没反应**：操作者 open_id 是否为白名单；飞书卡片回调是否到达
- **容器内 Claude 报错**：`docker exec feishu-claude-agent env | grep ANTHROPIC`
- **容器内 OpenCode 报错**：`docker logs feishu-claude-agent` 看 serve 日志
- **会话错乱**：发 `/new` 重置
- **改了 systemd unit 文件**：先 `systemctl --user daemon-reload`，再 restart

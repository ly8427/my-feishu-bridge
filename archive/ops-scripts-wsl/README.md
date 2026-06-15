# 归档：WSL 运维 / 补丁脚本（已废弃）

这些脚本是 feishu-bridge **早期开发阶段**（2026-05 ~ 06）在 WSL 宿主机上
使用的临时运维和补丁工具。它们曾散落在 Windows 的
`C:\Users\liu\ZCodeProject\` 目录里，硬编码了 `/home/liu/...` 路径，依赖
bash / pgrep / `/proc` / Unix 信号，**只能在「这一台 WSL 机器」上跑**。

## 状态：已被取代，不再使用

这些脚本的功能已被以下跨平台方案取代：

| 旧脚本 | 取代者 |
|--------|--------|
| `restart_all.sh`, `kill_all_bridge.sh`, `cleanup_bridge.sh`, `start_bridge*.sh`, `switch_model.sh`, `start_ds_bridge.sh` | `cli.py`（`start` / `stop` / `restart` / `up` / `down`） |
| `check_agent.sh`, `check_serve.sh`, `test_opencode_direct.sh`, `sse_test.sh`, `timing_test.sh`, `_test_resume.sh`, `e2e_test.py` | `cli.py check` / `cli.py status` / `cli.py doctor` |
| `dump_now.sh`, `thread_dump.sh` | `cli.py logs -f` |
| `fix_sse.py`, `fix_debug.py`, `fix_debug2.py`, `patch_bridge.py`, `instrument_opencode.py`, `_fix_session.py`, `fix_startup.sh`, `simulate_bridge.sh` | **修复内容已合入** `bridge.py` / `agent_runner_opencode.py`（见 git 历史），脚本仅留档 |
| `cleanup_container.sh` | `cli.py down` + `cli.py up` |
| `commit_msg.txt`, `feishu-bridge.service` | 历史记录；service unit 现由 `~/.config/systemd/user/` 管理 |

## 为什么保留

- 历史参考：记录了 SSE 阻塞、permission.asked 崩溃、PID 单实例等问题的调试过程
- 防止回退：将来若类似问题复现，可对照当年的排查思路

## 为什么不要再跑它们

- 硬编码 `/home/liu/projects/claudeWorkSpace/feishu-bridge`，换机器/换用户即失效
- 依赖 `pgrep` / `pkill` / `kill -USR1` / `/proc`，Windows/macOS 无对应物
- 部分 `.py` 是**一次性补丁**（运行一次改源码），目标源码早已包含其修复

**请用项目根目录的 `cli.py` 管理桥接器，用 `agent_readme.md` 指导环境适配。**

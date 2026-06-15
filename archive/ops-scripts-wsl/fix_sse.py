#!/usr/bin/env python3
"""Replace chunk-based SSE read with line-based (more reliable)."""
import sys

TARGET = "/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py"

with open(TARGET, "r") as f:
    content = f.read()

old = '''        # 6) Read SSE events
        line_buf = b""
        while not _stop_event.is_set():
            chunk = resp.read(4096)
            if not chunk:
                time.sleep(0.05)
                continue
            line_buf += chunk
            while b"\\n\\n" in line_buf:
                raw, line_buf = line_buf.split(b"\\n\\n", 1)
                raw_text = raw.decode("utf-8", errors="replace").strip()
                for line in raw_text.split("\\n"):
                    line = line.strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            evt = json.loads(data_str)
                            if _on_event(evt):
                                return
                        except json.JSONDecodeError:
                            pass'''

new = '''        # 6) Read SSE events (line-based — fixes buffering issue with read())
        sys.stderr.write("[DEBUG] SSE read loop started\\n")
        sys.stderr.flush()
        event_lines = []
        while not _stop_event.is_set():
            try:
                raw = resp.fp.readline()
            except Exception as _e:
                sys.stderr.write(f"[DEBUG] SSE read exception: {_e}\\n")
                sys.stderr.flush()
                break
            if not raw:
                sys.stderr.write("[DEBUG] SSE stream ended\\n")
                sys.stderr.flush()
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\\n")
            if line == "":
                if event_lines:
                    for el in event_lines:
                        if el.startswith("data: "):
                            try:
                                evt = json.loads(el[6:])
                                if _on_event(evt):
                                    return
                            except json.JSONDecodeError:
                                pass
                    event_lines = []
            else:
                event_lines.append(line)'''

assert old in content, "OLD SSE BLOCK NOT FOUND!"
content = content.replace(old, new, 1)
print("OK: replaced SSE read loop")

with open(TARGET, "w") as f:
    f.write(content)
print("Done:", TARGET)

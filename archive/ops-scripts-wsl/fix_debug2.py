#!/usr/bin/env python3
"""Fix the SECOND buggy debug line (emitting confirm_request)."""
f = open("/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py")
c = f.read()
f.close()

# Find the second debug line
for i, line in enumerate(c.split("\n")):
    if "DEBUG" in line and "emitting confirm_request" in line:
        print(f"Line {i+1}: {repr(line)}")
        fixed = '            sys.stderr.write(f"[DEBUG] emitting confirm_request cid={cid} tool={tool_name}\\n")'
        c = c.replace(line, fixed, 1)
        print(f"Fixed: {repr(fixed)}")
        break
else:
    print("NOT FOUND - checking all DEBUG lines:")
    for i, line in enumerate(c.split("\n")):
        if "DEBUG" in line:
            print(f"  Line {i+1}: {repr(line)}")
    exit(1)

open("/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py", "w").write(c)
print("Done")

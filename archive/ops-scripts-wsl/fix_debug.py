#!/usr/bin/env python3
"""Fix the debug line that crashed permission.asked handler."""
import sys
f = open("/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py")
c = f.read()
f.close()

# The buggy debug line has a double-escaped newline
# Replace it with a proper one
old = 'sys.stderr.write(f"[DEBUG] permission.asked: type={perm_type} id={perm_id}\\\n")'
new = 'sys.stderr.write(f"[DEBUG] permission.asked: type={perm_type} id={perm_id}\\n")'

if old in c:
    c = c.replace(old, new, 1)
    print("Fixed double-escaped newline")
elif new in c:
    print("Already correct")
else:
    print("Pattern not found, checking...")
    # Find the line
    for i, line in enumerate(c.split("\n")):
        if "DEBUG" in line and "permission.asked" in line:
            print(f"Line {i+1}: {repr(line)}")
            # Just rewrite it correctly
            c = c.replace(line, '            sys.stderr.write(f"[DEBUG] permission.asked: type={perm_type} id={perm_id}\\n")', 1)
            print(f"Fixed line {i+1}")
            break
    else:
        print("DEBUG line not found at all!")
        sys.exit(1)

open("/home/liu/projects/claudeWorkSpace/feishu-bridge/agent_runner_opencode.py", "w").write(c)
print("Done")

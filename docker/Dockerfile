# Container that runs Claude Code CLI AND OpenCode in isolation.
# It mounts ONLY the workspace dir (see docker-compose), so the agent cannot
# touch anything else on the host.
#
# Base on python:3.12-slim (clean pip, no distro-pinned anyio to conflict with
# the SDK + mcp), then add Node.js for the Claude Code CLI.
FROM python:3.12-slim-bookworm

# Node.js (for the Claude Code CLI) + tools the agent expects (git, ripgrep).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git ca-certificates ripgrep gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI. Clean the npm cache in the SAME layer so it never lands in
# an image layer (cleaning it in a later layer wouldn't shrink the image).
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# OpenCode CLI (from GitHub releases).
RUN curl -sL -o /tmp/opencode.tar.gz \
        https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-x64.tar.gz \
    && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/opencode \
    && rm /tmp/opencode.tar.gz

# Agent SDK in an isolated venv (no system-package interference; clean resolve).
RUN python3 -m venv /opt/agent \
    && /opt/agent/bin/pip install --no-cache-dir claude-agent-sdk==0.2.93
ENV PATH="/opt/agent/bin:${PATH}"

# Agent runner code.
WORKDIR /app
COPY agent_runner.py /app/agent_runner.py
COPY agent_runner_opencode.py /app/agent_runner_opencode.py

# Keep the container alive so the host can `docker exec` turns into it.
CMD ["sleep", "infinity"]

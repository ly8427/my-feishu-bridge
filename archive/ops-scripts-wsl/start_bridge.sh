#!/bin/bash
export FEISHU_ENV_FILE=/home/liu/.secrets/feishu-bridge.env
cd /home/liu/projects/claudeWorkSpace/feishu-bridge
exec .venv/bin/python3 bridge.py

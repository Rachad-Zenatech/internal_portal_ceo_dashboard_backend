#!/usr/bin/env bash
cd /mnt/c/dev/enterprise_system/mcp-server
export APP_ENV=development
exec ./venv/bin/python server.py

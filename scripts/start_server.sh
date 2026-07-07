#!/bin/bash
# 快速单独拉起 state server（--allow-mock）。日常请优先用 openclaw/serve.sh
# 统一管理常驻服务；本脚本供 ns-3 桥调试等只需要 state server 的场景。
cd "$(dirname "$0")/.."
nohup .venv/bin/python state_server/server.py --allow-mock > /tmp/state_server.log 2>&1 &
echo $! > /tmp/state_server.pid
sleep 5
lsof -i :5001 2>/dev/null | head -3
curl -s http://localhost:5001/health
echo ""
echo "EXIT:$?"

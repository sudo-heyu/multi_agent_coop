#!/bin/bash
cd /Users/heyu/Developer/ap/multi_agent_coop
nohup .venv/bin/python state_server/server.py --allow-mock > /tmp/state_server.log 2>&1 &
echo $! > /tmp/state_server.pid
sleep 5
lsof -i :5001 2>/dev/null | head -3
curl -s http://localhost:5001/health
echo ""
echo "EXIT:$?"

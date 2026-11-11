#!/usr/bin/env bash
# 本地（无 Docker）直接用系统 Python 启动两个服务，纯标准库、离线可用。
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p manufacturer/data warehouse/data logs

echo "启动生产商示例站  -> http://localhost:8081"
PORT=8081 python3 -I manufacturer/server.py > logs/manufacturer.log 2>&1 &
MFG_PID=$!
echo "启动仓库质控端    -> http://localhost:8080"
PORT=8080 python3 -I warehouse/server.py > logs/warehouse.log 2>&1 &
WH_PID=$!

trap 'echo; echo "停止服务..."; kill "$MFG_PID" "$WH_PID" 2>/dev/null || true' INT TERM
echo "两个服务已启动。浏览器打开 http://localhost:8080 ，Ctrl+C 停止。"
wait

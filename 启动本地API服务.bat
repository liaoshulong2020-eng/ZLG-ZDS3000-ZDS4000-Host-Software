@echo off
chcp 65001 >nul
title 致远示波器本地控制 API
cd /d "%~dp0"
echo 本地控制接口: http://127.0.0.1:8000/docs
"python" "run_api_server.py" --scope-ip 192.168.1.100 --port 8000
pause

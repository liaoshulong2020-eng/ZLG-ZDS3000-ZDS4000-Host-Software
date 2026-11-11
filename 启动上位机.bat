@echo off
chcp 65001 >nul
title 致远示波器专业上位机
cd /d "%~dp0"
start "" "pythonw" "run_app.py"
exit

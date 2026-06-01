@echo off
start "SOLO API" cmd /k "cd /d D:\智联枢纽 && python solo_api.py"
start "守护进程" cmd /k "cd /d D:\智联枢纽 && python nexus_daemon.py"
start "Web服务" cmd /k "cd /d D:\智联枢纽 && python web_server.py"
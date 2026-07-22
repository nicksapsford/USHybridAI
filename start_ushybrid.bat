@echo off
title USHybrid A.I. - Port 5044
cd /d C:\Users\abc\Desktop\USHybridAI
start /min "USHybrid A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_us.py
start /min "USHybrid A.I. Engine" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_us.py
timeout /t 5 /nobreak >nul
start http://localhost:5044

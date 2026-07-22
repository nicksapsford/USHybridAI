@echo off
title USHybrid A.I. Dashboard - Port 5044
cd /d C:\Users\abc\Desktop\USHybridAI
start /min "USHybrid A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_us.py
timeout /t 5 /nobreak >nul
start http://localhost:5044

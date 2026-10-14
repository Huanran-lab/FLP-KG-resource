@echo off
cd /d "%~dp0.."
REM Example: Desktop Bolt URI; password from .env
".venv\Scripts\python.exe" scripts\chat_kg_web.py --uri bolt://localhost:7687 %*

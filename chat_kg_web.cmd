@echo off
cd /d "%~dp0.."
REM Set NEO4J_PASSWORD in .env (copy from .env.example)
".venv\Scripts\python.exe" scripts\chat_kg_web.py %*

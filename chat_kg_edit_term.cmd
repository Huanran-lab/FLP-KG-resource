@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\chat_kg.py --uri bolt://localhost:7687 %*

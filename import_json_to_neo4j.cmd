@echo off
cd /d "%~dp0\.."
echo Usage examples:
echo   import curated KG:
echo   .venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir data\edited
echo.
echo   dry-run first:
echo   .venv\Scripts\python.exe scripts\import_json_to_neo4j.py --dir data\edited --dry-run
".venv\Scripts\python.exe" scripts\import_json_to_neo4j.py %*
if errorlevel 1 pause

@echo off
setlocal
cd /d "%~dp0\.."
set "PY=C:\Users\UserVik\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" tools\run_module.py autobot.real_market_scraper --probe "beton m300" --sources avito --headed --manual-wait-sec 300
pause

@echo off
rem StreamVault: record live streams of the channels from settings.json. Stop with Ctrl+C.
cd /d "%~dp0"
python -m streamvault record %*
if errorlevel 1 pause

@echo off
cd /d "%~dp0"
python radar_lima_publico.py --watch 3600 --publish
pause

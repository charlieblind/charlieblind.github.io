@echo off
cd /d "%~dp0"
python radar_lima_publico.py --watch 1800 --publish
pause

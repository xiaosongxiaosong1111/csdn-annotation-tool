@echo off
setlocal
cd /d "%~dp0"
python csdn_annotation_ui.py --host 127.0.0.1 --port 8765
pause

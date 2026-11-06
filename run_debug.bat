@echo off
rem bom-pdf debug run (console log)
set "PYTHONPATH=%~dp0src"
"%~dp0.venv\Scripts\python.exe" -m bom_pdf %*
pause

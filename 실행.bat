@echo off
rem bom-pdf run (no console window)
set "PYTHONPATH=%~dp0src"
start "" "%~dp0.venv\Scripts\pythonw.exe" -m bom_pdf %*

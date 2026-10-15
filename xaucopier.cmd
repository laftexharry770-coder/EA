@echo off
rem xaucopier launcher: runs the CLI with this folder's own Python, so
rem nothing has to be installed. Usage:  xaucopier watch   (or  .\xaucopier watch
rem in PowerShell). The exit code is passed through unchanged -- see
rem docs\RUNBOOK.md for what each one means.
setlocal
set "PYTHONPATH=%~dp0src"
"%~dp0.venv\Scripts\python.exe" -m xaucopier.cli.main %*
exit /b %ERRORLEVEL%

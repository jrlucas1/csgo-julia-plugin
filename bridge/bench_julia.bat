@echo off
rem Compara GPU x CPU sozinho (uns 2 minutos):  bench_julia.bat
rem Feche o run_julia.bat antes, pra nao brigarem pela GPU/CPU.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "VPY=%CD%\.venv-julia\Scripts\python.exe"
if not exist "%VPY%" (
  echo Rode primeiro: setup_julia.bat
  exit /b 1
)
"%VPY%" bench_julia.py
pause

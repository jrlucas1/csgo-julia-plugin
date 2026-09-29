@echo off
rem Roda o bridge com o Julia na GPU, no mesmo PC do servidor.
rem   run_julia.bat                 -> bridge (servidor: jev_bridge_host "127.0.0.1")
rem   run_julia.bat --device cpu    -> forca o CPU (outros argumentos passam direto pro bridge.py)
rem   run_julia.bat --test          -> manda estados falsos pro bridge local
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set "VPY=%CD%\.venv-julia\Scripts\python.exe"
if not exist "%VPY%" (
  echo Rode primeiro: setup_julia.bat
  exit /b 1
)
if /i "%~1"=="--test" goto :test
if not defined JULIA_CPU_THREADS set JULIA_CPU_THREADS=4
echo ==^> no servidor use:  jev_bridge_host "127.0.0.1"
echo ==^> overlay no OBS, Fonte de Navegador:  http://127.0.0.1:27501/
"%VPY%" bridge.py --decider julia --julia-path Julia-1 --device cuda --stance rule --bind 127.0.0.1 --overlay-port 27501 %*
exit /b %errorlevel%

:test
"%VPY%" fake_plugin.py --tickrate 128 --hz 32
exit /b %errorlevel%

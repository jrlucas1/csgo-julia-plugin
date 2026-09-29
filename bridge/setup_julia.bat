@echo off
rem Instala tudo pro Julia no Windows, com GPU NVIDIA (CUDA) quando der:  setup_julia.bat
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set PYTHONUTF8=1

rem ---- 1) Python 3.11+ (prefere 3.12: e a versao com torch+CUDA mais garantido no Windows)
set "PY="
for %%V in (3.12 3.13 3.11 3.14) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY goto :nopython
echo ==^> usando:
%PY% --version

rem ---- 2) ambiente virtual
if not exist ".venv-julia\Scripts\python.exe" (
  %PY% -m venv .venv-julia
  if errorlevel 1 goto :fail
)
set "VPY=%CD%\.venv-julia\Scripts\python.exe"
"%VPY%" -m pip install -q --upgrade pip "huggingface_hub>=1.3,<2.0"
if errorlevel 1 goto :fail

rem ---- 3) pesos do Julia (uns 550 MB), so na primeira vez
if exist "Julia-1\.baixado" goto :downloaded
echo ==^> baixando SupersonicLabs/Julia-1 do Hugging Face, uns 550 MB...
"%VPY%" -c "from huggingface_hub import snapshot_download; snapshot_download('SupersonicLabs/Julia-1', local_dir='Julia-1')"
if errorlevel 1 goto :fail
type nul > "Julia-1\.baixado"
goto :torch
:downloaded
echo ==^> Julia-1 ja baixado

rem ---- 4) torch com CUDA (o torch normal do PyPI no Windows e so CPU)
:torch
set "CUDA_IDX="
where nvidia-smi >nul 2>&1
if errorlevel 1 (
  echo Aviso: nvidia-smi nao encontrado. Sem driver da NVIDIA o Julia vai rodar no CPU.
  goto :julia
)
for %%C in (cu130 cu128 cu126) do (
  if not defined CUDA_IDX (
    echo ==^> instalando torch com CUDA, indice %%C. Pode demorar: sao uns 2-3 GB...
    "%VPY%" -m pip install -q --upgrade torch --index-url https://download.pytorch.org/whl/%%C
    "%VPY%" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1 && set "CUDA_IDX=%%C"
  )
)
if not defined CUDA_IDX echo Aviso: nao consegui um torch com CUDA funcionando. O Julia vai rodar no CPU.

rem ---- 5) pacote julia (vem dentro da pasta do modelo)
:julia
echo ==^> instalando o pacote julia...
"%VPY%" -m pip install -q -e .\Julia-1
if errorlevel 1 goto :fail
"%VPY%" -m pip install -q "huggingface_hub>=1.3,<2.0"
if errorlevel 1 goto :fail

rem Se o julia trocou o torch por uma versao sem CUDA, reinstala a MESMA versao com CUDA.
if defined CUDA_IDX (
  "%VPY%" -c "import torch, sys, subprocess; v = torch.__version__.split('+')[0]; sys.exit(0) if torch.cuda.is_available() else (print('==> o julia trocou o torch; reinstalando torch', v, 'com CUDA...'), sys.exit(subprocess.call([sys.executable, '-m', 'pip', 'install', '-q', '--force-reinstall', '--no-deps', 'torch==' + v, '--index-url', 'https://download.pytorch.org/whl/!CUDA_IDX!'])))"
)

rem ---- 6) confere
"%VPY%" -c "import torch; from julia import load_model; ok = torch.cuda.is_available(); print('==> julia OK | torch', torch.__version__, '| GPU:', torch.cuda.get_device_name(0) if ok else 'NAO (vai rodar no CPU)')"
if errorlevel 1 goto :fail

echo.
echo Pronto!
echo   Rodar o bridge:        run_julia.bat
echo   Testar sem o jogo:     run_julia.bat --test     (em outra janela, com o bridge rodando)
echo   Comparar GPU x CPU:    bench_julia.bat
exit /b 0

:nopython
echo Precisa de Python 3.11 ou mais novo.
echo Instale com:   winget install Python.Python.3.12
echo ou pelo python.org, marcando "Add python.exe to PATH". Depois rode este script de novo.
exit /b 1

:fail
echo.
echo Deu erro no passo acima. Copia as mensagens e manda pro Claude.
exit /b 1

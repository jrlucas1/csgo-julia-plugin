#!/usr/bin/env bash
# Instala tudo pro Julia num comando só (macOS/Linux):  bash setup_julia.sh
set -euo pipefail
cd "$(dirname "$0")"

# Python 3.11+
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Precisa de Python 3.11 ou mais novo."
  echo "No Mac:  brew install python@3.12   (ou baixe em python.org) e rode este script de novo."
  exit 1
fi
echo "==> usando $($PY --version)"

[ -d .venv-julia ] || "$PY" -m venv .venv-julia
# shellcheck disable=SC1091
source .venv-julia/bin/activate
python -m pip install -q --upgrade pip "huggingface_hub>=1.3,<2.0"   # o transformers 5.0 do Julia exige hub 1.x

if [ ! -f Julia-1/.baixado ]; then
  echo "==> baixando SupersonicLabs/Julia-1 do Hugging Face (~550 MB)..."
  python -c "from huggingface_hub import snapshot_download; snapshot_download('SupersonicLabs/Julia-1', local_dir='Julia-1')"
  touch Julia-1/.baixado
else
  echo "==> Julia-1 já baixado"
fi

echo "==> instalando o pacote julia (puxa o torch, pode demorar)..."
python -m pip install -q -e ./Julia-1
python -m pip install -q "huggingface_hub>=1.3,<2.0"   # garante a versão compatível no fim
python -c "from julia import load_model; print('==> julia OK')"

echo
echo "Pronto! Para rodar o bridge com o Julia:  bash run_julia.sh"
echo "Para testar sem o jogo (em outro terminal): bash run_julia.sh --test"

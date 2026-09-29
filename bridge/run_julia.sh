#!/usr/bin/env bash
# Roda o bridge com o Julia.  bash run_julia.sh          -> ouve a rede (0.0.0.0:27500)
#                             bash run_julia.sh --test   -> manda estados falsos pro bridge local
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv-julia ]; then echo "Rode primeiro: bash setup_julia.sh"; exit 1; fi
# shellcheck disable=SC1091
source .venv-julia/bin/activate
if [ "${1:-}" = "--test" ]; then
  shift; exec python fake_plugin.py "$@"
fi
# 4 threads + postura por regra foi o mais rápido no MacBook (p50 ~64 ms, contra ~107 ms
# com 8 threads e a 2ª pergunta). Para voltar a perguntar a postura: --stance model
export JULIA_CPU_THREADS="${JULIA_CPU_THREADS:-4}"
IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)
[ -n "$IP" ] && echo "==> no servidor use:  jev_bridge_host \"$IP\""
[ -n "$IP" ] && echo "==> overlay no OBS (Fonte de Navegador):  http://$IP:27501/"
exec python bridge.py --decider julia --julia-path Julia-1 --device cpu --stance rule --bind 0.0.0.0 --overlay-port 27501 "$@"

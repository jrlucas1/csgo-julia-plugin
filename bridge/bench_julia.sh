#!/usr/bin/env bash
# Compara configurações do Julia sozinho:  bash bench_julia.sh
# (sobe o bridge, roda o fake_plugin 10 s, derruba, e passa para a próxima)
cd "$(dirname "$0")"
if [ ! -d .venv-julia ]; then echo "Rode primeiro: bash setup_julia.sh"; exit 1; fi
source .venv-julia/bin/activate

run() {
  local name="$1" port="$2"; shift 2
  local log; log=$(mktemp)
  python bridge.py --decider julia --julia-path Julia-1 --port "$port" --quiet "$@" >"$log" 2>&1 &
  local pid=$!
  for _ in $(seq 1 180); do
    grep -q "ouvindo UDP" "$log" && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if ! grep -q "ouvindo UDP" "$log"; then
    echo "$name: o bridge não subiu. Últimas linhas:"; tail -5 "$log"; kill "$pid" 2>/dev/null; return
  fi
  local out; out=$(python fake_plugin.py --port "$port" --duration 10)
  kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  printf "%-28s %s | %s\n" "$name" \
    "$(echo "$out" | grep 'ida e volta' | sed 's/.*: *//; s/ (relógio.*//')" \
    "$(echo "$out" | grep '^respostas' | sed 's/.*: *//')"
  grep -q "usando cpu" "$log" && echo "   (mps não funcionou, caiu pro cpu)"
  rm -f "$log"
}

echo "Testando (uns 15-30 s por configuração)..."
echo
JULIA_CPU_THREADS=8 run "cpu, 8 threads"               27601 --device cpu
JULIA_CPU_THREADS=4 run "cpu, 4 threads"               27602 --device cpu
JULIA_CPU_THREADS=4 run "cpu, 4 threads, sem postura"  27603 --device cpu --stance rule
JULIA_CPU_THREADS=4 run "mps (GPU)"                    27604 --device mps
echo
echo "Menor p50 = mais rápido. Me manda esse resultado."

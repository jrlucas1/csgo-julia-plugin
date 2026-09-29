#!/usr/bin/env python3
"""
Compara configurações do Julia sozinho (Windows, Mac ou Linux):
sobe o bridge, roda o fake_plugin 10 s, derruba, e passa para a próxima.

  Windows:  bench_julia.bat
  direto:   .venv-julia\\Scripts\\python bench_julia.py   (ou .venv-julia/bin/python no Mac)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def run(name: str, port: int, extra: list[str], env_extra: dict[str, str], decider: str) -> None:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", **env_extra)
    cmd = [sys.executable, "bridge.py", "--decider", decider, "--port", str(port),
           "--stance", "rule", "--quiet", "--stats-every", "3", *extra]
    if decider == "julia":
        cmd += ["--julia-path", "Julia-1"]
    p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", env=env)
    lines: list[str] = []
    ready = threading.Event()

    def reader() -> None:
        for line in p.stdout:
            lines.append(line.rstrip())
            if "ouvindo UDP" in line:
                ready.set()

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.time() + 300   # 1ª vez na GPU pode demorar (inicialização do CUDA)
    while not ready.is_set() and p.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    if not ready.is_set():
        p.kill()
        print(f"{name:<18} o bridge não subiu. Últimas linhas:")
        for line in lines[-6:]:
            print("   " + line)
        return

    fp = subprocess.run([sys.executable, "fake_plugin.py", "--port", str(port), "--duration", "10",
                         "--tickrate", "128", "--hz", "32"],
                        cwd=HERE, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    time.sleep(0.5)
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()

    rtt = re.search(r"ida e volta\s*:\s*p50 ([\d.]+) ms\s+p95 ([\d.]+) ms", fp.stdout)
    resp = re.search(r"respostas\s*:\s*\d+ \((\d+)%", fp.stdout)
    models = [m.group(1) for line in lines for m in [re.search(r"modelo p50\s+([\d.]+)", line)] if m]
    model = f"{float(models[-1]):5.1f} ms" if models else "    ?"
    rtt_s = f"p50 {float(rtt.group(1)):5.1f} ms  p95 {float(rtt.group(2)):5.1f} ms" if rtt else "?"
    resp_s = f"{resp.group(1)}%" if resp else "?"
    print(f"{name:<18} modelo {model} | ida e volta {rtt_s} | respostas {resp_s}")
    if any("não funcionou" in line for line in lines):
        print("   (esse device não funcionou e caiu pro cpu; veja o setup)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decider", default="julia", help="só para testar este script (ex.: rules)")
    args = ap.parse_args()
    os.chdir(HERE)

    configs: list[tuple[str, list[str], dict[str, str]]] = []
    if sys.platform != "darwin":
        configs.append(("GPU (cuda)", ["--device", "cuda"], {}))
    configs += [
        ("CPU, 4 threads", ["--device", "cpu"], {"JULIA_CPU_THREADS": "4"}),
        ("CPU, 8 threads", ["--device", "cpu"], {"JULIA_CPU_THREADS": "8"}),
    ]
    print("Testando (uns 20-40 s por configuração; o estado vem a 32 Hz, como 128 tick / 4)...\n")
    for i, (name, extra, env_extra) in enumerate(configs):
        run(name, 27610 + i, extra, env_extra, args.decider)
    print("\nMenor 'ida e volta' = mais rápido. Me manda esse resultado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

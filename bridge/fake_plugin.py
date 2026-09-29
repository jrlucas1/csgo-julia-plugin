#!/usr/bin/env python3
"""
fake_plugin.py — finge ser o jev_bot.sp para testar o bridge sem abrir o jogo.

Manda estados falsos por UDP no mesmo formato do plugin, num cenário de duelo
que se repete (inimigo escondido fazendo barulho de passo -> aparece longe ->
mira em você -> você toma dano -> inimigo chega perto -> some e recarrega),
recebe "A <acao> <tick> <run|walk>",
mede a ida e volta e manda o rtt de volta no campo 12, como o plugin faz.

  python fake_plugin.py                       # 10 s, 16 Hz, 127.0.0.1:27500
  python fake_plugin.py --host 192.168.0.50   # bridge no MacBook
  python fake_plugin.py --burst 5             # 5 estados de uma vez: testa o descarte de velhos
  python fake_plugin.py --delay-send 400 3 3  # simula travamento na IDA: +400 ms dos 3 s aos 6 s
  python fake_plugin.py --pause-rx 3 2        # simula travamento na VOLTA: para de ler respostas
                                              # aos 3 s por 2 s (as respostas se acumulam)

Sai com código 1 se o bridge não responder nada.
"""

from __future__ import annotations

import argparse
import collections
import heapq
import math
import socket
import threading
import time

TICKRATE = 64.0            # ajustável com --tickrate
ENGINE_T0_MS = 3_600_000   # finge um servidor ligado há 1 h (GetEngineTime)


def pct(v: list[float], q: float) -> float:
    if not v:
        return float("nan")
    s = sorted(v)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class FakeBot:
    """Estado mínimo do bot, reagindo (grosseiramente) às ações recebidas."""

    CYCLE = 8.0  # segundos

    def __init__(self) -> None:
        self.hp = 100
        self.clip = 30
        self.reserve = 90
        self.action = "hold"
        self.action_until = 0.0
        self.hurt_at = -100.0
        self.x = 0.0
        self.walk = False
        self.last_seen_at = -100.0   # quando o inimigo foi visto pela última vez
        self.last_seen_dir = "none"
        self.sounds: list[tuple[str, str, float, float]] = []  # (tipo, direção, dist_m, quando)

    def apply(self, action: str, now: float, walk: bool = False) -> None:
        self.action = action
        self.walk = walk
        self.action_until = now + 0.35

    def current_action(self, now: float) -> str:
        return self.action if now < self.action_until else "hold"

    def step(self, t: float, dt: float, now: float) -> list[tuple]:
        """Avança a simulação e devolve os inimigos (userid, hp, dist, vis, aim)."""
        phase = t % self.CYCLE
        if phase < dt:  # começo de um ciclo: "novo round"
            self.hp, self.clip, self.reserve = 100, 30, 90
            self.last_seen_at, self.last_seen_dir, self.sounds = -100.0, "none", []
        act = self.current_action(now)

        def hear(kind: str, direction: str, dist: float) -> None:
            self.sounds.append((kind, direction, dist, now))

        if phase < 2.0:
            enemy = (7, 100, 30.0, 0, 0)
            # inimigo correndo atrás da parede: passos à frente-esquerda
            if 1.0 <= phase < 1.9 and int(phase * 4) != int((phase - dt) * 4):
                hear("step", "front_left", 14.0)
        elif phase < 3.0:
            enemy = (7, 100, 25.0, 1, 0)
        elif phase < 4.0:
            enemy = (7, 100, 22.0, 1, 1)
            if int(phase * 10) != int((phase - dt) * 10):
                hear("shot", "front", 22.0)
            if 3.5 <= phase < 3.5 + dt:
                self.hp = max(1, self.hp - 27)
                self.hurt_at = now
        elif phase < 6.0:
            enemy = (7, 85, 6.0, 1, 1)
            if 5.0 <= phase < 5.0 + dt:
                self.hp = max(1, self.hp - 30)
                self.hurt_at = now
        else:
            enemy = (7, 85, 12.0, 0, 0)
            if 6.5 <= phase < 6.5 + dt:
                hear("reload", "left", 12.0)

        # atirar gasta bala (~10 tiros/s com AK) quando há inimigo visível
        if enemy[3] and act in ("hold", "strafe_shoot", "crouch_spray", "peek_left", "peek_right", "push") \
                and self.clip > 0:
            self.clip = max(0, self.clip - max(1, round(10 * dt)))
        if act == "reload" or self.clip == 0:
            refill = min(30 - self.clip, self.reserve)
            self.clip += refill
            self.reserve -= refill
        if act == "peek_left":
            self.x -= 250 * dt
        elif act == "peek_right":
            self.x += 250 * dt
        if enemy[3]:
            self.last_seen_at, self.last_seen_dir = now, "front"
        self.sounds = [x for x in self.sounds if now - x[3] <= 3.0][-4:]
        return [enemy]

    def encode(self, tick: int, now: float, rtt_ms: int, enemies: list[tuple],
               net_ms: int = -1, t_ms: int = 0, stale: int = 0) -> str:
        hurt = 99999 if self.hurt_at < 0 else min(99999, int((now - self.hurt_at) * 1000))
        s = (f"S {tick} {self.hp} 100 {self.clip} {self.reserve} ak47 "
             f"{self.x:.0f} 0 0 {self.current_action(now)} {hurt} {rtt_ms} {net_ms} {t_ms} {stale}")
        for (uid, hp, dist, vis, aim) in enemies:
            if vis:
                seen_ago, direction = 0, "front"
            elif self.last_seen_at > 0:
                seen_ago, direction = min(99999, int((now - self.last_seen_at) * 1000)), self.last_seen_dir
            else:
                seen_ago, direction = 99999, "none"
            if not vis:  # jev_fair_info 1: sem hp/distância de quem não está visível
                hp, dist = -1, -1.0
            s += f"|E {uid} {hp} {dist:.1f} {vis} {aim} {seen_ago} {direction}"
        for (kind, direction, dist, when) in reversed(self.sounds):
            s += f"|H {kind} {direction} {dist:.0f} {int((now - when) * 1000)}"
        return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27500)
    ap.add_argument("--hz", type=float, default=16.0, help="estados por segundo (plugin: 64/jev_state_every)")
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--burst", type=int, default=1, help="estados enviados de uma vez a cada envio")
    ap.add_argument("--tickrate", type=float, default=64.0)
    ap.add_argument("--max-cmd-age", type=int, default=16, help="igual ao jev_max_cmd_age do plugin (ticks)")
    ap.add_argument("--delay-send", type=float, nargs=3, metavar=("MS", "INICIO", "DURACAO"), default=None,
                    help="segura cada estado MS ms antes de enviar, nesse intervalo (simula atraso na ida)")
    ap.add_argument("--pause-rx", type=float, nargs=2, metavar=("INICIO", "DURACAO"), default=None,
                    help="para de ler respostas nesse intervalo (simula atraso na volta/servidor)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    global TICKRATE
    TICKRATE = args.tickrate

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((args.host, args.port))  # como o plugin: UDP "conectado"
    sock.settimeout(0.2)

    bot = FakeBot()
    t0 = time.perf_counter()
    lock = threading.Lock()
    sent_at: dict[int, float] = {}
    rtts_exact: list[float] = []
    actions = collections.Counter()
    stances = collections.Counter()
    last_rtt_ms = -1
    last_net_ms = -1
    rtt_max_win = -1   # igual ao plugin: maior RTT desde o último estado
    net_max_win = -1
    stale_drops = 0
    last_ack_tick = -1
    received = 0
    out_of_order = 0
    refused = 0
    stop = threading.Event()

    def now_tick() -> float:
        return (time.perf_counter() - t0) * TICKRATE

    def engine_ms() -> float:
        return ENGINE_T0_MS + (time.perf_counter() - t0) * 1000.0

    def rx() -> None:
        nonlocal last_rtt_ms, last_net_ms, rtt_max_win, net_max_win, stale_drops, last_ack_tick, received, \
            out_of_order, refused
        while not stop.is_set():
            if args.pause_rx:
                start, dur = args.pause_rx
                el = time.perf_counter() - t0
                if start <= el < start + dur:
                    time.sleep(0.01)   # "servidor travado": ninguém lê o socket
                    continue
            try:
                data = sock.recv(512)
            except socket.timeout:
                continue
            except ConnectionRefusedError:
                refused += 1  # bridge fora do ar (ICMP port unreachable)
                time.sleep(0.2)
                continue
            except OSError:
                return
            parts = data.decode(errors="replace").split()
            if len(parts) < 3 or parts[0] != "A":
                continue
            tick = int(parts[2])
            t_now = time.perf_counter()
            with lock:
                received += 1
                if tick < last_ack_tick:
                    out_of_order += 1
                    continue
                last_ack_tick = tick
                if len(parts) >= 5:   # igual ao plugin: RTT pelo relógio do motor
                    last_rtt_ms = round(engine_ms() - float(parts[4]))
                    last_net_ms = last_rtt_ms - int(parts[5]) if len(parts) >= 6 else -1
                else:
                    last_rtt_ms = round((int(now_tick()) - tick) / TICKRATE * 1000)
                    last_net_ms = -1
                rtt_max_win = max(rtt_max_win, last_rtt_ms)
                net_max_win = max(net_max_win, last_net_ms)
                if tick in sent_at:
                    rtts_exact.append((t_now - sent_at.pop(tick)) * 1000)
                if args.max_cmd_age > 0 and int(now_tick()) - tick > args.max_cmd_age:
                    stale_drops += 1   # velho demais: o plugin não aplica
                    continue
                actions[parts[1]] += 1
                stance = parts[3] if len(parts) >= 4 else "run"
                stances[stance] += 1
                bot.apply(parts[1], t_now, stance == "walk")
            if args.verbose:
                print(f"<- {parts[1]} (tick {tick})")

    th = threading.Thread(target=rx, daemon=True)
    th.start()

    period = 1.0 / args.hz
    sent = 0
    next_send = time.perf_counter()
    last_t = 0.0
    delayed: list[tuple[float, int, str]] = []   # (quando enviar, seq, mensagem)
    seq = 0

    def flush_delayed() -> None:
        nonlocal sent, refused
        while delayed and delayed[0][0] <= time.perf_counter():
            _, _, m = heapq.heappop(delayed)
            try:
                sock.send(m.encode())
            except ConnectionRefusedError:
                refused += 1
            sent += 1

    while True:
        now = time.perf_counter()
        t = now - t0
        if t >= args.duration:
            break
        flush_delayed()
        if now < next_send:
            wake = next_send if not delayed else min(next_send, delayed[0][0])
            time.sleep(max(0.0, wake - now))
            continue
        next_send += period
        dt = t - last_t
        last_t = t
        with lock:
            enemies = bot.step(t, max(dt, 1e-3), now)
        for b in range(args.burst):
            tick = int(now_tick()) - (args.burst - 1 - b)  # burst: ticks distintos, o último é o mais novo
            with lock:
                msg = bot.encode(tick, now, rtt_max_win, enemies,
                                 net_ms=net_max_win, t_ms=int(engine_ms()), stale=stale_drops)
                rtt_max_win = net_max_win = -1
                sent_at[tick] = time.perf_counter()
            if args.delay_send and args.delay_send[1] <= t < args.delay_send[1] + args.delay_send[2]:
                seq += 1
                heapq.heappush(delayed, (time.perf_counter() + args.delay_send[0] / 1000, seq, msg))
            else:
                try:
                    sock.send(msg.encode())
                except ConnectionRefusedError:
                    refused += 1
                sent += 1
            if args.verbose:
                print(f"-> {msg}")

    time.sleep(0.3)  # espera respostas atrasadas
    stop.set()
    th.join(timeout=1)

    print("\n=== fake_plugin: resumo ===")
    print(f"estados enviados : {sent} ({args.hz:g} Hz x burst {args.burst}, {args.duration:g} s)")
    print(f"respostas        : {received} ({received / max(sent, 1):.0%} dos estados)")
    if args.burst > 1:
        print(f"  (com burst, o esperado é ~1 resposta por grupo: ~{1 / args.burst:.0%})")
    print(f"fora de ordem    : {out_of_order}")
    print(f"descartadas velhas: {stale_drops} (mais de {args.max_cmd_age} ticks)")
    if refused:
        print(f"conexão recusada : {refused}x (bridge não estava ouvindo em algum momento)")
    if rtts_exact:
        print(f"ida e volta      : p50 {pct(rtts_exact, .5):.1f} ms  p95 {pct(rtts_exact, .95):.1f} ms  "
              f"max {max(rtts_exact):.1f} ms (relógio de parede)")
    print(f"ações            : {dict(actions.most_common())}")
    print(f"posturas         : {dict(stances.most_common())}")
    if received == 0:
        print("\nNENHUMA resposta. O bridge está rodando nesse host/porta? Firewall?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

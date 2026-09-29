#!/usr/bin/env python3
"""
bridge.py — o "cérebro" do jev_bot.

Recebe o estado do bot por UDP (vindo do plugin SourceMod), monta a lista de
ações legais, pergunta a um decisor qual ação tomar e responde "A <acao> <tick>"
para o endereço de origem.

Protocolo (ver jev_bot.sp):
  plugin -> bridge : S tick hp armor clip reserve arma x y z acao_atual hurt_ago_ms [rtt_ms net_ms t_ms stale_drops]
                     |E userid hp dist_m vis mira [seen_ago_ms direcao]   (hp/dist = -1 se não visível)
                     |H tipo direcao dist_m ago_ms                        (sons: step, shot, reload, jump)
  bridge -> plugin : A <acao> <tick_do_estado> <run|walk> [t_ms bridge_ms]

Decisores (--decider):
  rules  baseline sem modelo
  julia  Julia 1 (Supersonic Labs) via o pacote `julia` do repositório do modelo
  laya   Laya via semantic-operators (provider laya)
  local  qualquer causal LM do Hugging Face, 1 forward pass, escolhe pela letra
  http   servidor compatível com System One (raw JSON = PLACEHOLDER) ou SDK da TypeSafe

Exemplos:
  python bridge.py --decider rules
  python bridge.py --decider local --model Qwen/Qwen2.5-3B-Instruct
  python bridge.py --decider laya --bind 0.0.0.0          # no MacBook
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import select
import socket
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------

ACTIONS = [
    "hold", "peek_left", "peek_right", "strafe_shoot",
    "crouch_spray", "fall_back", "push", "reload", "face_sound",
]

# Descrições usadas pelos modelos. Curtas e sem ambiguidade entre si.
ACTION_DESCRIPTIONS = {
    "hold":         "Stay still and hold the current angle; shoot if an enemy appears.",
    "peek_left":    "Strafe left to peek; stop and shoot once an enemy is in view.",
    "peek_right":   "Strafe right to peek; stop and shoot once an enemy is in view.",
    "strafe_shoot": "Strafe left-right (A-D) and shoot between strafes; good in close fights.",
    "crouch_spray": "Crouch and spray; steadier aim at medium or long range.",
    "fall_back":    "Walk backwards away from the enemy, to break line of sight or survive.",
    "push":         "Walk forward toward the enemy aggressively.",
    "reload":       "Reload the weapon now.",
    "face_sound":   "Turn to face where the enemy was last heard or seen, and hold that angle.",
}

# Segunda pergunta: postura durante a ação.
STANCES = {
    "run":  "Move at full speed; your footsteps are loud and the enemy can hear you.",
    "walk": "Walk slowly and silently (shift); the enemy cannot hear your footsteps.",
}
STANCE_QUESTION = "Should the bot run or walk silently during this action?"

DIRECTION_WORDS = {
    "front": "in front of you", "front_left": "to your front-left", "left": "to your left",
    "back_left": "behind you on the left", "back": "behind you", "back_right": "behind you on the right",
    "right": "to your right", "front_right": "to your front-right", "none": "somewhere",
}
SOUND_WORDS = {"step": "footsteps", "shot": "a gunshot", "reload": "a reload", "jump": "a jump"}
NEVER_MS = 99999

ACTION_SECONDS = 0.35  # duração de cada ação no plugin

# Tamanho do pente por arma (sem o prefixo weapon_). Só para decidir se reload é legal.
CLIP_SIZE = {
    "ak47": 30, "m4a1": 30, "m4a1_silencer": 20, "galilar": 35, "famas": 25,
    "aug": 30, "sg556": 30, "awp": 10, "ssg08": 10, "scar20": 20, "g3sg1": 20,
    "deagle": 7, "glock": 20, "hkp2000": 13, "usp_silencer": 12, "p250": 13,
    "fiveseven": 20, "tec9": 18, "elite": 30, "cz75a": 12,
    "mac10": 30, "mp9": 30, "mp7": 30, "mp5sd": 30, "ump45": 25, "p90": 50,
    "bizon": 64, "nova": 8, "xm1014": 7, "mag7": 5, "sawedoff": 7,
    "negev": 150, "m249": 100,
}

# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------


@dataclass
class Enemy:
    userid: int
    hp: Optional[int]          # None quando o plugin esconde (jev_fair_info 1 e não visível)
    dist_m: Optional[float]    # idem
    visible: bool
    aiming_at_me: bool
    seen_ago_ms: int = NEVER_MS   # 0 = visível agora
    direction: str = "none"       # relativa ao olhar do bot


@dataclass
class Sound:
    kind: str        # step | shot | reload | jump
    direction: str
    dist_m: float
    ago_ms: int


@dataclass
class GameState:
    tick: int
    hp: int
    armor: int
    clip: int
    reserve: int
    weapon: str
    x: float
    y: float
    z: float
    action: str
    hurt_ago_ms: int
    rtt_ms: Optional[int] = None          # medido pelo plugin (-1 = ainda não há)
    net_ms: Optional[int] = None          # rtt - tempo do bridge (rede ida+volta + espera no servidor)
    t_ms: Optional[int] = None            # GetEngineTime() do servidor no envio, em ms
    stale_drops: Optional[int] = None     # comandos que o plugin descartou por velhos (acumulado)
    enemies: list[Enemy] = field(default_factory=list)
    sounds: list[Sound] = field(default_factory=list)

    @property
    def visible_enemies(self) -> list[Enemy]:
        return [e for e in self.enemies if e.visible]

    @property
    def nearest_visible(self) -> Optional[Enemy]:
        vis = self.visible_enemies
        return min(vis, key=lambda e: e.dist_m if e.dist_m is not None else 1e9) if vis else None

    def recent_enemy_info(self, within_ms: int) -> bool:
        """Ouviu algo ou viu alguém (que não está visível agora) há menos de within_ms."""
        return (any(s.ago_ms < within_ms for s in self.sounds)
                or any(not e.visible and e.seen_ago_ms < within_ms for e in self.enemies))


class ParseError(ValueError):
    pass


def parse_state(text: str) -> GameState:
    """Converte a linha do plugin em GameState. Tolera o campo rtt_ms ausente."""
    parts = text.strip().split("|")
    head = parts[0].split()
    if not head or head[0] != "S" or len(head) < 12:
        raise ParseError(f"cabeçalho inválido: {parts[0][:80]!r}")
    try:
        st = GameState(
            tick=int(head[1]), hp=int(head[2]), armor=int(head[3]),
            clip=int(head[4]), reserve=int(head[5]), weapon=head[6],
            x=float(head[7]), y=float(head[8]), z=float(head[9]),
            action=head[10], hurt_ago_ms=int(head[11]),
        )
        if len(head) >= 13:
            rtt = int(head[12])
            st.rtt_ms = rtt if rtt >= 0 else None
        if len(head) >= 16:
            net = int(head[13])
            st.net_ms = net if net >= 0 else None
            st.t_ms = int(head[14])
            st.stale_drops = int(head[15])
        for p in parts[1:]:
            f = p.split()
            if f and f[0] == "E" and len(f) in (6, 8):
                hp, dist = int(f[2]), float(f[3])
                st.enemies.append(Enemy(
                    userid=int(f[1]),
                    hp=hp if hp >= 0 else None,
                    dist_m=dist if dist >= 0 else None,
                    visible=f[4] == "1", aiming_at_me=f[5] == "1",
                    seen_ago_ms=int(f[6]) if len(f) == 8 else (0 if f[4] == "1" else NEVER_MS),
                    direction=f[7] if len(f) == 8 else "none",
                ))
            elif f and f[0] == "H" and len(f) == 5:
                st.sounds.append(Sound(kind=f[1], direction=f[2], dist_m=float(f[3]), ago_ms=int(f[4])))
            else:
                raise ParseError(f"bloco inválido: {p[:80]!r}")
    except (ValueError, IndexError) as e:
        if isinstance(e, ParseError):
            raise
        raise ParseError(str(e)) from e
    return st


def legal_actions(st: GameState) -> list[str]:
    """Ações que fazem sentido no estado atual (sempre >= 2, na ordem de ACTIONS)."""
    enemy_visible = bool(st.visible_enemies)
    has_ammo = st.clip > 0
    clip_max = CLIP_SIZE.get(st.weapon, 30)

    legal = []
    for a in ACTIONS:
        if a in ("strafe_shoot", "crouch_spray"):
            # só faz sentido atirar se tem inimigo visível e bala no pente
            if not (enemy_visible and has_ammo):
                continue
        elif a == "reload":
            if st.reserve <= 0 or st.clip < 0 or st.clip >= clip_max:
                continue
            # com inimigo na cara, só recarrega se o pente estiver quase vazio
            if enemy_visible and st.clip > clip_max // 4:
                continue
        elif a == "face_sound":
            # só com algo para encarar: som recente ou inimigo visto há pouco (e ninguém visível)
            if enemy_visible or not st.recent_enemy_info(3000):
                continue
        legal.append(a)
    return legal


def describe_state(st: GameState) -> str:
    """Texto em inglês para os modelos (Julia/Laya/local/http)."""
    lines = [
        "Counter-Strike 1v1 duel. You control one bot; aiming and shooting are automatic, "
        "you only choose the movement tactic for the next 0.35 seconds.",
        f"You: {st.hp} HP, {st.armor} armor, weapon {st.weapon} with {max(st.clip, 0)} bullets "
        f"in the magazine and {max(st.reserve, 0)} in reserve.",
        f"Current action: {st.action}.",
    ]
    if st.hurt_ago_ms < 99999:
        lines.append(f"You took damage {st.hurt_ago_ms / 1000:.1f} seconds ago.")
    else:
        lines.append("You have not taken damage this round.")
    if not st.enemies:
        lines.append("No enemies alive.")
    for i, e in enumerate(st.enemies, 1):
        where = DIRECTION_WORDS.get(e.direction, "somewhere")
        if e.visible:
            hp = f"{e.hp} HP, " if e.hp is not None else ""
            dist = f"{e.dist_m:.0f} m away " if e.dist_m is not None else ""
            aim = ", AIMING AT YOU" if e.aiming_at_me else ""
            lines.append(f"Enemy {i}: VISIBLE, {hp}{dist}{where}{aim}.")
        elif e.seen_ago_ms < NEVER_MS:
            lines.append(f"Enemy {i}: not visible; last seen {e.seen_ago_ms / 1000:.1f} s ago {where}.")
        else:
            lines.append(f"Enemy {i}: not visible and not seen this round.")
    if st.sounds:
        for snd in st.sounds:
            what = SOUND_WORDS.get(snd.kind, snd.kind)
            where = DIRECTION_WORDS.get(snd.direction, "somewhere")
            lines.append(f"You heard {what} {snd.ago_ms / 1000:.1f} s ago, about {snd.dist_m:.0f} m {where}.")
    else:
        lines.append("You hear nothing.")
    return "\n".join(lines)


QUESTION = "Which action should the bot take for the next 0.35 seconds?"

# ---------------------------------------------------------------------------
# Dispositivo
# ---------------------------------------------------------------------------


def pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"

# ---------------------------------------------------------------------------
# Decisores
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    action: str
    probs: dict[str, float] = field(default_factory=dict)
    stance: str = "run"
    stance_probs: dict[str, float] = field(default_factory=dict)


def rule_stance(st: GameState, action: str) -> str:
    """
    Postura por regra (rules, local, ou quando o modelo não responde a 2ª pergunta):
    anda em silêncio quando sabe que tem inimigo por perto mas não está vendo;
    corre para empurrar, fugir, recarregar ou quando já está trocando tiro.
    """
    if st.visible_enemies or action in ("push", "fall_back", "reload"):
        return "run"
    return "walk" if st.recent_enemy_info(5000) else "run"


class Decider:
    name = "base"
    ask_stance = False   # True: pergunta a postura ao modelo (2ª questão na mesma chamada)

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        raise NotImplementedError

    def _stance_fallback_warning(self, e: Exception) -> None:
        """Se o modelo recusar 2 perguntas numa chamada, desliga a 2ª e segue por regra."""
        self.ask_stance = False
        print(f"[{self.name}] o modelo não aceitou a pergunta de postura ({e!r}); "
              "usando postura por regra daqui em diante", file=sys.stderr)

    @staticmethod
    def _stance_from(ans: Optional[dict], st: GameState, action: str) -> tuple[str, dict[str, float]]:
        if not ans:
            return rule_stance(st, action), {}
        probs = {k: float(v) for k, v in (ans.get("probabilities") or {}).items()}
        choice = ans.get("choice")
        if choice not in STANCES:
            choice = max(probs, key=probs.get) if probs else rule_stance(st, action)
        return choice, probs

    @staticmethod
    def _fallback(legal: list[str], probs: dict[str, float]) -> str:
        """Ação de maior probabilidade entre as legais (defesa contra respostas fora da lista)."""
        best = max(legal, key=lambda a: probs.get(a, -1.0))
        return best if probs else legal[0]


class RulesDecider(Decider):
    """Baseline sem modelo. Serve para testar o encanamento e como comparação."""

    name = "rules"

    PEEK_COMMIT = 0.45  # s: um peek precisa durar mais que 1 decisão (~60 ms) para servir

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        self.next_peek_at = 0.0
        self.peek_side = "peek_left"
        self.peek_until = 0.0

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        def pick(*prefs: str) -> str:
            for a in prefs:
                if a in legal:
                    return a
            return "hold" if "hold" in legal else legal[0]

        enemy = st.nearest_visible
        now = time.monotonic()
        dec = self._decide_action(st, legal, pick, enemy, now)
        dec.stance = rule_stance(st, dec.action)
        return dec

    def _decide_action(self, st, legal, pick, enemy, now) -> Decision:
        if st.clip == 0:
            return Decision(pick("reload", "fall_back"))

        if enemy is not None:
            if st.hp < 40 and enemy.aiming_at_me:
                return Decision(pick("fall_back"))
            if enemy.dist_m < 8:
                return Decision(pick("strafe_shoot", "hold"))
            if enemy.dist_m > 20:
                return Decision(pick("crouch_spray", "hold"))
            return Decision(pick("hold"))

        # sem inimigo visível
        if st.hurt_ago_ms < 1500:
            self.peek_until = 0.0
            return Decision(pick("fall_back"))
        # ouviu passo/tiro há pouco: vira para o barulho e segura
        if "face_sound" in legal and any(s.kind in ("step", "shot") and s.ago_ms < 1500 for s in st.sounds):
            self.peek_until = 0.0
            return Decision("face_sound")
        if 0 <= st.clip < 10 and "reload" in legal:
            return Decision("reload")
        if now < self.peek_until:
            return Decision(pick(self.peek_side))
        if now >= self.next_peek_at:
            # a cada ~1.5-3 s dá uma espiada de ~0.45 s, alternando o lado
            self.next_peek_at = now + self.rng.uniform(1.5, 3.0)
            self.peek_side = "peek_right" if self.peek_side == "peek_left" else "peek_left"
            self.peek_until = now + self.PEEK_COMMIT
            return Decision(pick(self.peek_side))
        return Decision(pick("hold"))


def _choice_criteria(legal: list[str]) -> dict[str, str]:
    return {a: ACTION_DESCRIPTIONS[a] for a in legal}


class JuliaDecider(Decider):
    """
    Julia 1 (SupersonicLabs/Julia-1, Apache 2.0).

    API conferida no model card do Hugging Face em 2026-09-28:
        python -m pip install huggingface_hub
        python -c "from huggingface_hub import snapshot_download; snapshot_download('SupersonicLabs/Julia-1', local_dir='Julia-1')"
        python -m pip install -e ./Julia-1

        from julia import load_model
        engine = load_model("Julia-1", device="cpu", strict_encoding=True, max_length=8192, head_length=512)
        result = engine.predict(state=..., questions={"q": {"type": "choice", "instructions": ..., "criteria": {id: desc}}})
        result["answers"]["q"]["choice"], result["answers"]["q"]["probabilities"]

    O model card lista device "cpu" e "cuda" (GPU com BF16). "mps" NÃO é
    mencionado: aqui a gente tenta e, se falhar, cai para cpu com aviso.
    Obs.: o pacote se chama `julia`, o mesmo nome do PyJulia; não instale os
    dois no mesmo venv.
    """

    name = "julia"

    def __init__(self, path: str, device: str, max_length: int):
        try:
            from julia import load_model  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "pacote `julia` não encontrado. Siga o model card: snapshot_download para ./Julia-1 "
                "e `pip install -e ./Julia-1` (veja README)."
            ) from e
        if not hasattr(sys.modules.get("julia"), "load_model"):
            raise SystemExit("o `julia` importado não tem load_model — é o PyJulia? Use um venv separado.")

        kwargs = dict(strict_encoding=True, max_length=max_length, head_length=512)
        try:
            self.engine = load_model(path, device=device, **kwargs)
            if device != "cpu":
                # carregar pode dar certo e só a 1ª decisão quebrar (ex.: mps no Mac:
                # "Passed CPU tensor to MPS op"). Testa uma decisão antes de aceitar.
                self.engine.predict(state="test", questions={"q": {
                    "type": "choice", "instructions": "test", "criteria": {"a": "a", "b": "b"}}})
            self.device = device
        except Exception as e:  # noqa: BLE001
            if device == "cpu":
                raise
            print(f"[julia] device={device} não funcionou ({type(e).__name__}: {e}); usando cpu", file=sys.stderr)
            self.engine = load_model(path, device="cpu", **kwargs)
            self.device = "cpu"

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        questions = {"action": {
            "type": "choice",
            "instructions": QUESTION,
            "criteria": _choice_criteria(legal),
        }}
        if self.ask_stance:
            questions["stance"] = {"type": "choice", "instructions": STANCE_QUESTION, "criteria": dict(STANCES)}
        state = describe_state(st)
        try:
            result = self.engine.predict(state=state, questions=questions)
        except Exception as e:  # noqa: BLE001
            if not self.ask_stance:
                raise
            self._stance_fallback_warning(e)
            questions.pop("stance")
            result = self.engine.predict(state=state, questions=questions)
        ans = result["answers"]["action"]
        probs = {k: float(v) for k, v in ans.get("probabilities", {}).items()}
        action = ans.get("choice")
        if action not in legal:
            action = self._fallback(legal, probs)
        stance, sprobs = self._stance_from(result["answers"].get("stance"), st, action)
        return Decision(action, probs, stance, sprobs)


class LayaDecider(Decider):
    """
    Laya via semantic-operators (conferido no código-fonte de semantic-operators
    0.11.0 e laya 0.3.21 em 2026-09-28):

        import laya
        from semantic_operators import Choice
        from semantic_operators.providers.laya import Laya
        provider = Laya(laya.load("convaiinnovations/laya", device="cuda"))
        answers = provider.ask(state, {"action": Choice(instructions, {id: desc})})
        answers["action"].value, answers["action"].probabilities

    laya.load aceita device (cpu/cuda/mps) e subfolder (ex.: "multilingual").
    """

    name = "laya"

    def __init__(self, model: str, subfolder: Optional[str], device: str):
        try:
            import laya  # type: ignore
            from semantic_operators import Choice  # type: ignore
            from semantic_operators.providers.laya import Laya  # type: ignore
        except ImportError as e:
            raise SystemExit('instale: pip install -r requirements-laya.txt') from e
        self._Choice = Choice
        self.provider = Laya(laya.load(model, device=device, subfolder=subfolder))
        self.device = device

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        qs = {"action": self._Choice(QUESTION, _choice_criteria(legal))}
        if self.ask_stance:
            qs["stance"] = self._Choice(STANCE_QUESTION, dict(STANCES))
        state = describe_state(st)
        try:
            answers = self.provider.ask(state, qs)   # as 2 perguntas numa chamada só
        except Exception as e:  # noqa: BLE001
            if not self.ask_stance:
                raise
            self._stance_fallback_warning(e)
            qs.pop("stance")
            answers = self.provider.ask(state, qs)
        ans = answers["action"]
        probs = {k: float(v) for k, v in ans.probabilities.items()}
        action = ans.value if ans.value in legal else self._fallback(legal, probs)
        sa = answers.get("stance")
        stance, sprobs = self._stance_from(
            {"choice": sa.value, "probabilities": sa.probabilities} if sa is not None else None, st, action)
        return Decision(action, probs, stance, sprobs)


LETTERS = "ABCDEFGHIJ"


class LocalLMDecider(Decider):
    """
    Qualquer causal LM do Hugging Face. Rotula as opções com A-H, roda UMA
    forward pass e escolhe pela maior logit entre os tokens das letras
    ("A" e " A"), sem gerar texto.
    """

    name = "local"

    def __init__(self, model_id: str, device: str, dtype: str):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:
            raise SystemExit("instale: pip install -r requirements-local.txt") from e
        self.torch = torch
        torch_dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[dtype]
        if device == "cpu" and dtype != "fp32":
            print("[local] cpu: usando fp32 (bf16 no cpu costuma ser mais lento)", file=sys.stderr)
            torch_dtype = torch.float32
        self.device = device
        import transformers
        major_minor = tuple(int(x) for x in transformers.__version__.split(".")[:2])
        dtype_kw = "dtype" if major_minor >= (4, 56) else "torch_dtype"  # renomeado no 4.56
        self.tok = AutoTokenizer.from_pretrained(model_id)
        self.model = AutoModelForCausalLM.from_pretrained(model_id, **{dtype_kw: torch_dtype})
        self.model.to(device).eval()

        template = getattr(self.tok, "chat_template", None) or ""
        self.has_template = bool(template)
        self.template_kwargs = {"enable_thinking": False} if "enable_thinking" in template else {}

        # ids candidatos de cada letra: "A" e " A", só quando viram 1 token
        self.letter_ids: list[list[int]] = []
        for L in LETTERS:
            ids = set()
            for variant in (L, " " + L):
                enc = self.tok.encode(variant, add_special_tokens=False)
                if len(enc) == 1:
                    ids.add(enc[0])
            if not ids:
                raise SystemExit(f"o tokenizer de {model_id} não tem token único para {L!r}")
            self.letter_ids.append(sorted(ids))

    def _prompt(self, st: GameState, legal: list[str]) -> str:
        opts = "\n".join(f"{LETTERS[i]}. {a}: {ACTION_DESCRIPTIONS[a]}" for i, a in enumerate(legal))
        user = (
            f"{describe_state(st)}\n\n{QUESTION}\n{opts}\n\n"
            f"Answer with a single letter ({', '.join(LETTERS[:len(legal)])})."
        )
        if self.has_template:
            msgs = [
                {"role": "system", "content": "You are a fast tactical controller for a Counter-Strike bot."},
                {"role": "user", "content": user},
            ]
            return self.tok.apply_chat_template(
                msgs, tokenize=False, add_generation_prompt=True, **self.template_kwargs
            ) + "Answer:"
        return user + "\nAnswer:"

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        torch = self.torch
        legal = legal[: len(LETTERS)]
        text = self._prompt(st, legal)
        inputs = self.tok(text, return_tensors="pt", add_special_tokens=not self.has_template).to(self.device)
        with torch.inference_mode():
            logits = self.model(**inputs).logits[0, -1].float()
        scores = torch.stack([
            torch.logsumexp(logits[self.letter_ids[i]], dim=0) for i in range(len(legal))
        ])
        p = torch.softmax(scores, dim=0).tolist()
        probs = {a: float(p[i]) for i, a in enumerate(legal)}
        action = max(probs, key=probs.get)
        # postura por regra: uma 2ª forward pass dobraria a latência
        return Decision(action, probs, rule_stance(st, action))


class HttpDecider(Decider):
    """
    Servidor compatível com a API "System One" (Jev/Kev/OpenJev local, TypeSafe).

    --http-backend typesafe  -> usa o SDK oficial via semantic-operators (API conferida no
                                código de semantic-operators 0.11.0: TypeSafe(TypeSafeClient(), model)).
                                Precisa de TYPESAFE_API_KEY. NÃO é local.
    --http-backend raw       -> POST JSON em --http-url. **PLACEHOLDER**: eu não sei o endpoint nem
                                o schema exato do seu servidor. O corpo abaixo copia o formato de
                                pergunta que Julia/Laya aceitam ({"type","instructions","criteria"}) e
                                espera {"answers": {"action": {"choice", "probabilities"}}} de volta.
                                Ajuste _raw_request/_raw_parse pelo README do servidor.
    """

    name = "http"

    def __init__(self, backend: str, url: Optional[str], model: str, timeout: float):
        self.backend = backend
        self.timeout = timeout
        self.model = model
        if backend == "typesafe":
            try:
                from typesafe_sdk import TypeSafeClient  # type: ignore
                from semantic_operators import Choice  # type: ignore
                from semantic_operators.providers.typesafe import TypeSafe  # type: ignore
            except ImportError as e:
                raise SystemExit('instale: pip install -r requirements-http.txt') from e
            self._Choice = Choice
            self.client = TypeSafeClient()  # lê TYPESAFE_API_KEY do ambiente
            self.provider = TypeSafe(self.client, model=model)
        else:
            if not url:
                raise SystemExit("--http-url é obrigatório com --http-backend raw")
            import urllib.request
            self._urlreq = urllib.request
            self.url = url
            self.api_key = os.environ.get("SYSTEM_ONE_API_KEY") or os.environ.get("TYPESAFE_API_KEY")

    # ---- raw (PLACEHOLDER) ----
    def _raw_request(self, st: GameState, legal: list[str]) -> dict:
        # PLACEHOLDER: confira o schema no README do seu servidor.
        questions = {"action": {
            "type": "choice",
            "instructions": QUESTION,
            "criteria": _choice_criteria(legal),
        }}
        if self.ask_stance:
            questions["stance"] = {"type": "choice", "instructions": STANCE_QUESTION, "criteria": dict(STANCES)}
        return {"model": self.model, "state": describe_state(st), "questions": questions}

    @staticmethod
    def _raw_parse(body: dict) -> tuple[Optional[str], dict[str, float], Optional[dict]]:
        # PLACEHOLDER: formato de resposta igual ao de Julia/Laya.
        ans = body["answers"]["action"]
        return (ans.get("choice"), {k: float(v) for k, v in ans.get("probabilities", {}).items()},
                body["answers"].get("stance"))

    def decide(self, st: GameState, legal: list[str]) -> Decision:
        stance_ans = None
        if self.backend == "typesafe":
            qs = {"action": self._Choice(QUESTION, _choice_criteria(legal))}
            if self.ask_stance:
                qs["stance"] = self._Choice(STANCE_QUESTION, dict(STANCES))
            answers = self.provider.ask(describe_state(st), qs)
            ans = answers["action"]
            probs = {k: float(v) for k, v in ans.probabilities.items()}
            action = ans.value
            sa = answers.get("stance")
            if sa is not None:
                stance_ans = {"choice": sa.value, "probabilities": sa.probabilities}
        else:
            data = json.dumps(self._raw_request(st, legal)).encode()
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            req = self._urlreq.Request(self.url, data=data, headers=headers, method="POST")
            with self._urlreq.urlopen(req, timeout=self.timeout) as resp:
                action, probs, stance_ans = self._raw_parse(json.loads(resp.read()))
        if action not in legal:
            action = self._fallback(legal, probs)
        stance, sprobs = self._stance_from(stance_ans, st, action)
        return Decision(action, probs, stance, sprobs)


def build_decider(args: argparse.Namespace, device: str) -> Decider:
    d = _build_decider(args, device)
    # --stance auto: pergunta ao modelo quando ele responde várias perguntas numa chamada
    # (julia/laya/http); rules e local usam a regra.
    if args.stance == "model" or (args.stance == "auto" and args.decider in ("julia", "laya", "http")):
        d.ask_stance = True
    return d


def _build_decider(args: argparse.Namespace, device: str) -> Decider:
    if args.decider == "rules":
        return RulesDecider(seed=args.seed)
    if args.decider == "julia":
        return JuliaDecider(args.julia_path, device, args.julia_max_length)
    if args.decider == "laya":
        return LayaDecider(args.laya_model, args.laya_subfolder, device)
    if args.decider == "local":
        return LocalLMDecider(args.model, device, args.dtype)
    if args.decider == "http":
        return HttpDecider(args.http_backend, args.http_url, args.http_model, args.http_timeout)
    raise SystemExit(f"decisor desconhecido: {args.decider}")

# ---------------------------------------------------------------------------
# UDP
# ---------------------------------------------------------------------------


def disable_udp_connreset(sock: socket.socket) -> None:
    """
    No Windows, um ICMP "port unreachable" (ex.: servidor reiniciou) faz o
    próximo recvfrom() levantar ConnectionResetError (WSAECONNRESET).
    SIO_UDP_CONNRESET = FALSE desliga isso. O socket.ioctl do CPython só aceita
    3 códigos fixos, então usamos WSAIoctl via ctypes.
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    SIO_UDP_CONNRESET = 0x9800000C
    ws2 = ctypes.WinDLL("ws2_32")
    ws2.WSAIoctl.argtypes = [
        ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    ws2.WSAIoctl.restype = ctypes.c_int
    flag = wintypes.BOOL(False)
    returned = wintypes.DWORD(0)
    rc = ws2.WSAIoctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(flag), ctypes.sizeof(flag),
                      None, 0, ctypes.byref(returned), None, None)
    if rc != 0:
        print(f"[bridge] aviso: WSAIoctl(SIO_UDP_CONNRESET) falhou (erro {ws2.WSAGetLastError()})",
              file=sys.stderr)


class UdpInbox:
    """
    Recebe no próprio loop principal, sem thread: espera com select() e, quando
    tem dado, drena o socket inteiro (não bloqueante) e fica só com o datagrama
    mais novo. Antes havia uma thread de recepção: se ela ficava sem o GIL
    (disputando com o modelo), os estados empilhavam no buffer do kernel, e o
    bridge_ms não via esse tempo porque só começava a contar na leitura.

    Com um único bot só existe um remetente; com vários, fica o último que chegou.
    """

    MAX_DRAIN = 10_000

    def __init__(self, sock: socket.socket):
        self.sock = sock
        sock.setblocking(False)
        self.dropped = 0      # datagramas lidos e jogados fora por existir um mais novo
        self.max_burst = 0    # maior número de datagramas encontrados de uma vez (backlog)

    def wait(self, timeout: float) -> bool:
        try:
            r, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            return False
        return bool(r)

    def drain(self) -> Optional[tuple[tuple, bytes, float, int]]:
        """(remetente, dados, instante da leitura, quantos datagramas havia) do mais novo."""
        last = None
        n = 0
        while n < self.MAX_DRAIN:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                continue  # Windows: ICMP de um envio anterior
            except OSError:
                break
            n += 1
            last = (addr, data)
        if last is None:
            return None
        self.dropped += n - 1
        self.max_burst = max(self.max_burst, n)
        return last[0], last[1], time.perf_counter(), n


class ArrivalClock:
    """
    Estima o atraso de CHEGADA (plugin -> bridge) usando o relógio do servidor:
    cada estado traz t_ms = GetEngineTime() no envio. offset = relógio_local - t_ms
    tem uma parte fixa (diferença entre os relógios + o menor atraso de rede) e uma
    parte variável (fila, rede, buffer). O menor offset numa janela deslizante
    aproxima a parte fixa; o que passar disso é atraso extra de chegada.
    """

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self.win: deque[tuple[float, float]] = deque()   # (instante, offset), offsets crescentes
        self.last_t_ms: Optional[int] = None

    def observe(self, t_ms: Optional[int], now: float) -> Optional[float]:
        if t_ms is None:
            return None
        if self.last_t_ms is not None and t_ms < self.last_t_ms - 1000:
            self.win.clear()   # relógio do servidor voltou: servidor reiniciou
        self.last_t_ms = t_ms
        off = now * 1000.0 - t_ms
        while self.win and self.win[-1][1] >= off:
            self.win.pop()
        self.win.append((now, off))
        while self.win and now - self.win[0][0] > self.window_s:
            self.win.popleft()
        return off - self.win[0][1]

# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class Stats:
    """Janela de 5 s com p50/p95 de cada medida."""

    def __init__(self, every: float = 5.0):
        self.every = every
        self.reset()

    def reset(self) -> None:
        self.t0 = time.perf_counter()
        self.model_ms: list[float] = []
        self.bridge_ms: list[float] = []
        self.arrival_ms: list[float] = []
        self.net_ms: list[float] = []
        self.rtt_ms: list[float] = []
        self.decisions = 0
        self.errors = 0

    def maybe_print(self, inbox: UdpInbox, prefix: str, stale_drops: int) -> bool:
        """Imprime a janela se já deu o tempo; devolve True quando imprimiu (e zerou)."""
        dt = time.perf_counter() - self.t0
        if dt < self.every:
            return False

        def fmt(name: str, v: list[float], with_max: bool = False) -> str:
            if not v:
                return f"{name} -"
            out = f"{name} p50 {pct(v, .5):5.1f} p95 {pct(v, .95):5.1f}"
            return out + (f" máx {max(v):6.0f}" if with_max else "")

        print(
            f"[{prefix}] {self.decisions / dt:5.1f} dec/s | "
            f"{fmt('chegada', self.arrival_ms)} | {fmt('modelo', self.model_ms)} | "
            f"{fmt('bridge', self.bridge_ms)} | {fmt('rede', self.net_ms)} | "
            f"{fmt('total(rtt)', self.rtt_ms, with_max=True)} ms | "
            f"descartados {inbox.dropped} (rajada máx {inbox.max_burst}) | "
            f"velhos no plugin {stale_drops} | erros {self.errors}",
            flush=True,
        )
        hint = self.diagnose(stale_drops=stale_drops)
        if hint:
            print(f"[{prefix}] ATRASO: {hint}", flush=True)
        inbox.dropped = 0
        inbox.max_burst = 0
        self.reset()
        return True

    def diagnose(self, limit_ms: float = 100.0, stale_drops: int = 0) -> Optional[str]:
        """Diz em qual trecho está o atraso quando o RTT passa do limite."""
        arrival95 = pct(self.arrival_ms, .95) if self.arrival_ms else 0.0
        if stale_drops and arrival95 < limit_ms and (not self.bridge_ms or pct(self.bridge_ms, .95) < limit_ms):
            # respostas chegaram velhas no plugin, mas a ida e o bridge estavam rápidos
            worst = f", pior RTT {max(self.rtt_ms):.0f} ms" if self.rtt_ms else ""
            return (f"{stale_drops} comandos chegaram velhos no plugin{worst}; ida e bridge normais -> "
                    "atraso na VOLTA ou no servidor (rede de volta, fila de callbacks da extensão, hitch)")
        if not self.rtt_ms or pct(self.rtt_ms, .95) < limit_ms:
            return None
        legs = {
            "na CHEGADA (servidor -> bridge: envio do plugin, rede ou buffer do socket)":
                pct(self.arrival_ms, .95) if self.arrival_ms else 0.0,
            "DENTRO do bridge (modelo/processamento)":
                pct(self.bridge_ms, .95) if self.bridge_ms else 0.0,
            "na VOLTA ou no servidor (rede de volta, fila de callbacks da extensão, hitch)":
                max(0.0, (pct(self.net_ms, .95) if self.net_ms else 0.0)
                    - (pct(self.arrival_ms, .95) if self.arrival_ms else 0.0)),
        }
        where, ms = max(legs.items(), key=lambda kv: kv[1])
        return f"total p95 {pct(self.rtt_ms, .95):.0f} ms; maior parte {where}, ~{ms:.0f} ms"

# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------


_DIR_PT = {
    "front": "na frente", "front_left": "frente-esquerda", "left": "à esquerda", "back_left": "atrás-esquerda",
    "back": "atrás", "back_right": "atrás-direita", "right": "à direita", "front_right": "frente-direita",
    "none": "?",
}
_SOUND_PT = {"step": "passos", "shot": "tiro", "reload": "recarga", "jump": "pulo"}


def situation(st: GameState) -> str:
    """Resumo curto em português para o log e o overlay."""
    ne = st.nearest_visible
    if ne is not None:
        dist = f"{ne.dist_m:.0f}m" if ne.dist_m is not None else "?"
        return f"inimigo {dist}{' MIRANDO' if ne.aiming_at_me else ''}"
    if st.sounds:
        s = st.sounds[0]
        return f"ouviu {_SOUND_PT.get(s.kind, s.kind)} {_DIR_PT.get(s.direction, '?')} ~{s.dist_m:.0f}m"
    seen = [e for e in st.enemies if e.seen_ago_ms < NEVER_MS]
    if seen:
        e = min(seen, key=lambda e: e.seen_ago_ms)
        return f"viu {_DIR_PT.get(e.direction, '?')} há {e.seen_ago_ms / 1000:.1f}s"
    return "sem visão"


_DIR_EN = {
    "front": "ahead", "front_left": "front-left", "left": "left", "back_left": "back-left",
    "back": "behind", "back_right": "back-right", "right": "right", "front_right": "front-right",
    "none": "?",
}
_SOUND_EN = {"step": "footsteps", "shot": "a shot", "reload": "a reload", "jump": "a jump"}


def situation_en(st: GameState) -> str:
    """Short summary in English for the OBS overlay (the audience sees this one)."""
    ne = st.nearest_visible
    if ne is not None:
        dist = f"{ne.dist_m:.0f}m" if ne.dist_m is not None else "?"
        return f"enemy {dist}" + (" · AIMING AT HER" if ne.aiming_at_me else "")
    if st.sounds:
        s = st.sounds[0]
        return f"heard {_SOUND_EN.get(s.kind, s.kind)} {_DIR_EN.get(s.direction, '?')} ~{s.dist_m:.0f}m"
    seen = [e for e in st.enemies if e.seen_ago_ms < NEVER_MS]
    if seen:
        e = min(seen, key=lambda e: e.seen_ago_ms)
        return f"last seen {_DIR_EN.get(e.direction, '?')} {e.seen_ago_ms / 1000:.1f}s ago"
    return "no contact"


class OverlayServer:
    """
    Serve o overlay bonito (overlay.html) e o estado atual em /state, para o OBS
    ("Fonte de Navegador"). Roda numa thread à parte e só copia um dicionário
    pequeno a cada decisão, então não atrapalha o loop principal.
    """

    def __init__(self, host: str, port: int):
        import http.server

        self.lock = threading.Lock()
        self.state: dict = {}
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.html")
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in ("/", "/overlay.html"):
                    try:
                        with open(html_path, "rb") as f:
                            self._send(200, f.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send(404, b"overlay.html nao encontrado", "text/plain")
                elif path == "/state":
                    with server.lock:
                        body = json.dumps(server.state).encode()
                    self._send(200, body, "application/json")
                else:
                    self._send(404, b"", "text/plain")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="overlay-http", daemon=True).start()

    def update(self, st: GameState, dec: Decision, model_ms: float, decider: str, device: str) -> None:
        d = {
            "t": time.time(), "tick": st.tick, "action": dec.action, "stance": dec.stance,
            "probs": {k: round(v, 4) for k, v in dec.probs.items()},
            "situation": situation_en(st), "hp": st.hp, "clip": st.clip, "reserve": st.reserve,
            "weapon": st.weapon, "model_ms": None if math.isnan(model_ms) else round(model_ms, 1),
            "rtt_ms": st.rtt_ms, "decider": decider, "device": device,
        }
        with self.lock:
            self.state = d

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def write_overlay(path: str, st: GameState, dec: Decision, model_ms: float) -> None:
    """
    Texto para o OBS (fonte "Texto" com "Ler do arquivo"): a ação atual e as
    probabilidades em barrinhas. Escreve num .tmp e troca, para o OBS nunca ler
    um arquivo pela metade.
    """
    walking = " (andando em silêncio)" if dec.stance == "walk" else ""
    lines = [f"JULIA >> {dec.action.upper()}{walking}"]
    for a, p in sorted(dec.probs.items(), key=lambda kv: -kv[1])[:5]:
        lines.append(f"{a:<13}{'#' * round(p * 20):<20} {p:4.0%}")
    lines.append(f"{st.hp} HP | pente {max(st.clip, 0)} | {situation(st)}"
                 + ("" if math.isnan(model_ms) else f" | {model_ms:.0f} ms"))
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass  # o OBS pode estar segurando o arquivo no Windows; tenta na próxima


def warmup(decider: Decider, n: int) -> None:
    fake = parse_state("S 1 100 100 30 90 ak47 0 0 0 hold 99999 -1|E 2 -1 -1 0 0 1200 left|H step left 12 800")
    legal = legal_actions(fake)
    times = []
    for _ in range(n):
        t = time.perf_counter()
        decider.decide(fake, legal)
        times.append((time.perf_counter() - t) * 1000)
    if times:
        print(f"[bridge] warm-up {n}x: " + ", ".join(f"{x:.1f}" for x in times) + " ms", flush=True)


def _install_sigterm() -> None:
    """SIGTERM (Linux/macOS) e Ctrl+Break (Windows) saem como Ctrl+C: fecha o JSONL direito."""
    import signal

    def handler(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def run(args: argparse.Namespace) -> None:
    _install_sigterm()
    device = pick_device(args.device) if args.decider in ("julia", "laya", "local") else "-"
    print(f"[bridge] decisor={args.decider} device={device}", flush=True)
    decider = build_decider(args, device if device != "-" else "cpu")
    warmup(decider, args.warmup)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    disable_udp_connreset(sock)
    if args.rcvbuf > 0:
        # buffer pequeno: se algo travar, o kernel descarta em vez de guardar segundos de estados velhos
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.rcvbuf)
    sock.bind((args.bind, args.port))
    print(f"[bridge] ouvindo UDP {args.bind}:{args.port} (max {args.max_hz} Hz)", flush=True)

    overlay = None
    if args.overlay_port:
        overlay = OverlayServer(args.bind, args.overlay_port)
        host = "127.0.0.1" if args.bind in ("127.0.0.1", "0.0.0.0") else args.bind
        print(f"[bridge] overlay pro OBS: http://{host}:{overlay.port}/  (Fonte de Navegador, 1920x1080)",
              flush=True)

    inbox = UdpInbox(sock)
    clock = ArrivalClock()
    stale_seen: Optional[int] = None
    stale_window = 0
    stats = Stats(args.stats_every)
    jsonl = open(args.log_jsonl, "a", encoding="utf-8") if args.log_jsonl else None
    min_interval = 1.0 / args.max_hz if args.max_hz > 0 else 0.0
    last_decision_at = 0.0
    last_action: dict[tuple, str] = {}
    last_stance: dict[tuple, str] = {}
    changed_at: dict[tuple, float] = {}
    overlay_at: dict[tuple, float] = {}
    prev_for_overlay: dict[tuple, str] = {}
    parse_errors = 0

    try:
        while True:
            if stats.maybe_print(inbox, args.decider, stale_window):
                stale_window = 0
            if not inbox.wait(0.5):
                continue
            item = inbox.drain()
            if item is None:
                continue

            # limite de taxa: espera o intervalo mínimo e drena de novo (fica com o mais novo)
            wait = min_interval - (time.perf_counter() - last_decision_at)
            if wait > 0:
                time.sleep(wait)
                newer = inbox.drain()
                if newer is not None:
                    inbox.dropped += 1
                    item = newer
            addr, data, t_recv, _burst = item

            try:
                st = parse_state(data.decode("utf-8", "replace"))
            except ParseError as e:
                parse_errors += 1
                if parse_errors <= 5 or parse_errors % 100 == 0:
                    print(f"[bridge] estado inválido de {addr}: {e}", file=sys.stderr)
                continue

            legal = legal_actions(st)
            prev = last_action.get(addr)
            committed = (args.commit_ms > 0 and prev in legal
                         and (time.perf_counter() - changed_at.get(addr, 0.0)) * 1000 < args.commit_ms)
            try:
                if committed:
                    # --commit-ms: mantém a ação anterior por um tempo mínimo, sem chamar o modelo
                    dec, model_ms = Decision(prev, stance=last_stance.get(addr, "run")), float("nan")
                else:
                    t_model = time.perf_counter()
                    dec = decider.decide(st, legal)
                    model_ms = (time.perf_counter() - t_model) * 1000
            except Exception as e:  # noqa: BLE001 — um erro do modelo não pode derrubar o bridge
                stats.errors += 1
                if stats.errors <= 3:
                    print(f"[bridge] erro no decisor: {e!r}; respondendo hold", file=sys.stderr)
                dec, model_ms = Decision("hold", stance=rule_stance(st, "hold")), float("nan")

            bridge_ms = (time.perf_counter() - t_recv) * 1000
            reply = f"A {dec.action} {st.tick} {dec.stance}"
            if st.t_ms is not None:
                # eco do relógio do servidor + nosso tempo: o plugin separa rede de processamento
                reply += f" {st.t_ms} {round(bridge_ms)}"
            try:
                sock.sendto(reply.encode(), addr)
            except OSError as e:
                print(f"[bridge] sendto {addr} falhou: {e}", file=sys.stderr)
                continue
            t_sent = time.perf_counter()
            last_decision_at = t_sent

            arrival_ms = clock.observe(st.t_ms, t_recv)
            stats.decisions += 1
            if not math.isnan(model_ms):
                stats.model_ms.append(model_ms)
            stats.bridge_ms.append(bridge_ms)
            if arrival_ms is not None:
                stats.arrival_ms.append(arrival_ms)
            if st.net_ms is not None:
                stats.net_ms.append(float(st.net_ms))
            if st.rtt_ms is not None:
                stats.rtt_ms.append(float(st.rtt_ms))
            if st.stale_drops is not None:
                if stale_seen is not None and st.stale_drops >= stale_seen:
                    stale_window += st.stale_drops - stale_seen
                stale_seen = st.stale_drops

            changed = last_action.get(addr) != dec.action or last_stance.get(addr) != dec.stance
            if not args.quiet and changed:
                top = ""
                if dec.probs:
                    p = dec.probs.get(dec.action)
                    top = f" p={p:.2f}" if p is not None else ""
                walk = " (andando)" if dec.stance == "walk" else ""
                was = last_action.get(addr, "-") + (" (andando)" if last_stance.get(addr) == "walk" else "")
                print(f"[acao] tick {st.tick}: {was} -> {dec.action}{walk}{top} "
                      f"(hp {st.hp}, pente {st.clip}, {situation(st)}, modelo {model_ms:.1f} ms)", flush=True)
            if last_action.get(addr) != dec.action:
                changed_at[addr] = t_sent
            last_action[addr] = dec.action
            last_stance[addr] = dec.stance

            if args.overlay_file and (changed
                                      or t_sent - overlay_at.get(addr, 0.0) > 0.25):
                write_overlay(args.overlay_file, st, dec, model_ms)
                overlay_at[addr] = t_sent
                prev_for_overlay[addr] = dec.action

            if overlay:
                overlay.update(st, dec, model_ms, args.decider, device)

            if jsonl:
                jsonl.write(json.dumps({
                    "t": time.time(), "addr": f"{addr[0]}:{addr[1]}", "tick": st.tick,
                    "decider": args.decider, "device": device,
                    "action": dec.action, "probs": dec.probs, "legal": legal,
                    "stance": dec.stance, "stance_probs": dec.stance_probs,
                    "model_ms": None if math.isnan(model_ms) else round(model_ms, 3),
                    "bridge_ms": round(bridge_ms, 3),
                    "arrival_ms": None if arrival_ms is None else round(arrival_ms, 3),
                    "burst": _burst, "rtt_ms": st.rtt_ms, "net_ms": st.net_ms,
                    "stale_drops": st.stale_drops, "raw": data.decode("utf-8", "replace"),
                }) + "\n")
    except KeyboardInterrupt:
        print("\n[bridge] saindo", flush=True)
    finally:
        sock.close()
        if overlay:
            overlay.close()
        if jsonl:
            jsonl.close()


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Bridge UDP entre o jev_bot (SourceMod) e um modelo de decisão.")
    ap.add_argument("--bind", default="127.0.0.1", help="IP para escutar (0.0.0.0 quando rodar em outra máquina)")
    ap.add_argument("--port", type=int, default=27500)
    ap.add_argument("--decider", choices=["rules", "julia", "laya", "local", "http"], default="rules")
    ap.add_argument("--device", default="auto", help="auto | cuda | mps | cpu")
    ap.add_argument("--max-hz", type=float, default=0.0,
                    help="máximo de decisões por segundo (0 = sem limite: o ritmo é o do modelo). "
                         "Um limite abaixo da taxa de estados do plugin ADICIONA atraso.")
    ap.add_argument("--rcvbuf", type=int, default=16384,
                    help="tamanho do buffer de recepção UDP em bytes (0 = padrão do sistema)")
    ap.add_argument("--warmup", type=int, default=3, help="decisões falsas na inicialização")
    ap.add_argument("--stats-every", type=float, default=5.0)
    ap.add_argument("--log-jsonl", default=None, help="arquivo JSONL com uma linha por decisão")
    ap.add_argument("--quiet", action="store_true", help="não imprime quando a ação muda")
    ap.add_argument("--overlay-port", type=int, default=0,
                    help="serve o overlay bonito pro OBS nessa porta (ex.: 27501; 0 = desligado)")
    ap.add_argument("--overlay-file", default=None,
                    help="arquivo de texto com a decisão atual, para o OBS ler (gravação de vídeo)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--stance", choices=["auto", "model", "rule"], default="auto",
                    help="postura run/walk: auto = modelo pergunta (julia/laya/http), regra para rules/local")
    ap.add_argument("--commit-ms", type=float, default=0.0,
                    help="tempo mínimo antes de trocar de ação (0 = decide livre a cada estado)")
    # julia
    ap.add_argument("--julia-path", default="Julia-1", help="pasta baixada com snapshot_download")
    ap.add_argument("--julia-max-length", type=int, default=1024,
                    help="tokens de contexto (os benchmarks publicados usaram 1024)")
    # laya
    ap.add_argument("--laya-model", default="convaiinnovations/laya")
    ap.add_argument("--laya-subfolder", default=None, help='ex.: "multilingual"')
    # local
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct")
    ap.add_argument("--dtype", choices=["bf16", "fp16", "fp32"], default="bf16")
    # http
    ap.add_argument("--http-backend", choices=["raw", "typesafe"], default="raw")
    ap.add_argument("--http-url", default=None)
    ap.add_argument("--http-model", default="jev-latest")
    ap.add_argument("--http-timeout", type=float, default=1.0)
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()

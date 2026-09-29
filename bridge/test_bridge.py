#!/usr/bin/env python3
"""
Testes do bridge, sem jogo e sem baixar modelos.

  python test_bridge.py            # tudo que der para rodar nesta máquina
  python test_bridge.py -v

- parser, ações legais, rules
- adaptadores julia / laya / http(raw): com modelos/servidores FALSOS que devolvem o
  formato documentado — testa o encanamento, não a qualidade do modelo
- local: um causal LM minúsculo e aleatório criado na hora (precisa de torch + transformers)
- ponta a ponta: sobe o bridge (rules) numa porta livre e roda o fake_plugin contra ele
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from argparse import Namespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bridge  # noqa: E402

STATE_VISIBLE = "S 1200 73 100 24 90 ak47 -1520 300 12 hold 450 31|E 5 100 18.4 1 1|E 6 40 30.0 0 0"
STATE_HIDDEN = "S 10 100 100 30 90 ak47 0 0 0 hold 99999 -1|E 5 100 25.0 0 0"


def have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


class ParseTests(unittest.TestCase):
    def test_parse_full(self):
        st = bridge.parse_state(STATE_VISIBLE)
        self.assertEqual((st.tick, st.hp, st.clip, st.reserve, st.weapon), (1200, 73, 24, 90, "ak47"))
        self.assertEqual(st.rtt_ms, 31)
        self.assertEqual(len(st.enemies), 2)
        self.assertTrue(st.enemies[0].visible and st.enemies[0].aiming_at_me)
        self.assertEqual(st.nearest_visible.userid, 5)

    def test_parse_without_rtt_field(self):
        st = bridge.parse_state("S 5 100 0 30 90 ak47 1 2 3 push 99999")
        self.assertIsNone(st.rtt_ms)
        self.assertEqual(st.enemies, [])

    def test_negative_rtt_is_none(self):
        self.assertIsNone(bridge.parse_state(STATE_HIDDEN).rtt_ms)

    def test_garbage(self):
        for bad in ["", "A hold 3", "S 1 2 3", "S x 100 100 30 90 ak47 0 0 0 hold 1 -1", STATE_HIDDEN + "|E 1 2"]:
            with self.assertRaises(bridge.ParseError, msg=bad):
                bridge.parse_state(bad)


class LegalTests(unittest.TestCase):
    def test_no_enemy_no_shooting_actions(self):
        legal = bridge.legal_actions(bridge.parse_state(STATE_HIDDEN))
        self.assertNotIn("crouch_spray", legal)
        self.assertNotIn("strafe_shoot", legal)
        self.assertNotIn("reload", legal)  # pente cheio
        self.assertIn("hold", legal)

    def test_visible_enemy(self):
        legal = bridge.legal_actions(bridge.parse_state(STATE_VISIBLE))
        self.assertIn("crouch_spray", legal)
        self.assertNotIn("reload", legal)  # 24/30 com inimigo visível

    def test_empty_clip(self):
        st = bridge.parse_state("S 1 100 100 0 90 ak47 0 0 0 hold 99999 -1|E 5 100 10.0 1 0")
        legal = bridge.legal_actions(st)
        self.assertIn("reload", legal)
        self.assertNotIn("crouch_spray", legal)

    def test_always_at_least_two_and_ordered(self):
        for s in (STATE_HIDDEN, STATE_VISIBLE, "S 1 100 100 0 0 ak47 0 0 0 hold 1 -1"):
            legal = bridge.legal_actions(bridge.parse_state(s))
            self.assertGreaterEqual(len(legal), 2)
            self.assertEqual(legal, [a for a in bridge.ACTIONS if a in legal])


class RulesTests(unittest.TestCase):
    def test_rules(self):
        d = bridge.RulesDecider()
        st = bridge.parse_state("S 1 100 100 0 90 ak47 0 0 0 hold 99999 -1")
        self.assertEqual(d.decide(st, bridge.legal_actions(st)).action, "reload")
        st = bridge.parse_state("S 1 30 0 20 90 ak47 0 0 0 hold 100 -1|E 5 100 10.0 1 1")
        self.assertEqual(d.decide(st, bridge.legal_actions(st)).action, "fall_back")
        st = bridge.parse_state("S 1 100 100 20 90 ak47 0 0 0 hold 99999 -1|E 5 100 5.0 1 0")
        self.assertEqual(d.decide(st, bridge.legal_actions(st)).action, "strafe_shoot")

    def test_peek_lasts_more_than_one_decision(self):
        d = bridge.RulesDecider()
        st = bridge.parse_state(STATE_HIDDEN)
        legal = bridge.legal_actions(st)
        first = d.decide(st, legal).action
        self.assertIn(first, ("peek_left", "peek_right"))
        self.assertEqual(d.decide(st, legal).action, first)  # ~60 ms depois, continua


class JuliaAdapterTests(unittest.TestCase):
    """Pacote `julia` falso com a API do model card: load_model(...).predict(state=, questions=)."""

    def setUp(self):
        self.calls = []
        calls = self.calls

        class Engine:
            def predict(self, state, questions):
                calls.append((state, questions))
                crit = questions["action"]["criteria"]
                ids = list(crit)
                probs = {k: (0.7 if k == "crouch_spray" else 0.3 / (len(ids) - 1)) for k in ids}
                return {"answers": {"action": {"type": "choice", "choice": "crouch_spray",
                                               "probabilities": probs, "max_probability": 0.7}}}

        mod = types.ModuleType("julia")
        mod.load_model = lambda path, device, **kw: (calls.append(("load", path, device, kw)), Engine())[1]
        sys.modules["julia"] = mod

    def tearDown(self):
        sys.modules.pop("julia", None)

    def test_request_and_response(self):
        d = bridge.JuliaDecider("Julia-1", "cpu", 1024)
        st = bridge.parse_state(STATE_VISIBLE)
        legal = bridge.legal_actions(st)
        dec = d.decide(st, legal)
        self.assertEqual(dec.action, "crouch_spray")
        _, path, device, kw = self.calls[0]
        self.assertEqual((path, device, kw["max_length"], kw["head_length"]), ("Julia-1", "cpu", 1024, 512))
        q = self.calls[1][1]["action"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(list(q["criteria"]), legal)
        self.assertTrue(2 <= len(legal) <= 20)


@unittest.skipUnless(have("semantic_operators"), "semantic-operators não instalado")
class LayaAdapterTests(unittest.TestCase):
    """Usa o provider Laya REAL do semantic-operators com um modelo falso (formato Jev/Laya)."""

    def setUp(self):
        self.seen = {}
        seen = self.seen

        class FakeAgent:
            def predict(self, state, request):
                seen["state"], seen["request"] = state, request
                ids = list(request["action"]["criteria"])
                rest = round(0.5 / (len(ids) - 1), 4)          # o 1º fica com ~50%: é o mais provável
                probs = {k: rest for k in ids}
                probs[ids[0]] = round(1.0 - rest * (len(ids) - 1), 4)
                answers = {"action": {"choice": ids[0], "probabilities": probs}}
                if "stance" in request:
                    answers["stance"] = {"choice": "walk", "probabilities": {"run": 0.3, "walk": 0.7}}
                return {"answers": answers, "model": "laya-rl-agent"}

        mod = types.ModuleType("laya")
        mod.load = lambda model, device=None, subfolder=None: (seen.update(load=(model, device, subfolder)), FakeAgent())[1]
        sys.modules["laya"] = mod

    def tearDown(self):
        sys.modules.pop("laya", None)

    def test_roundtrip(self):
        d = bridge.LayaDecider("convaiinnovations/laya", None, "mps")
        st = bridge.parse_state(STATE_HIDDEN)
        legal = bridge.legal_actions(st)
        dec = d.decide(st, legal)
        self.assertEqual(self.seen["load"], ("convaiinnovations/laya", "mps", None))
        self.assertEqual(self.seen["request"]["action"]["type"], "choice")
        self.assertEqual(dec.action, legal[0])
        self.assertAlmostEqual(sum(dec.probs.values()), 1.0, places=3)

    def test_stance_second_question(self):
        d = bridge.LayaDecider("convaiinnovations/laya", None, "cpu")
        d.ask_stance = True
        st = bridge.parse_state(STATE_HEARING)
        dec = d.decide(st, bridge.legal_actions(st))
        self.assertEqual(self.seen["request"]["stance"]["type"], "choice")
        self.assertEqual((dec.stance, dec.stance_probs), ("walk", {"run": 0.3, "walk": 0.7}))


class HttpRawTests(unittest.TestCase):
    """--http-backend raw contra um servidor local falso (formato PLACEHOLDER)."""

    def test_raw(self):
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got["body"], got["auth"] = body, self.headers.get("Authorization")
                ids = list(body["questions"]["action"]["criteria"])
                out = json.dumps({"answers": {"action": {"choice": ids[-1],
                                                         "probabilities": {k: 1 / len(ids) for k in ids}}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            os.environ["SYSTEM_ONE_API_KEY"] = "teste"
            d = bridge.HttpDecider("raw", f"http://127.0.0.1:{srv.server_port}/v1/system-one", "kev-local", 2.0)
            st = bridge.parse_state(STATE_VISIBLE)
            legal = bridge.legal_actions(st)
            dec = d.decide(st, legal)
            self.assertEqual(dec.action, legal[-1])
            self.assertEqual(got["auth"], "Bearer teste")
            self.assertEqual(got["body"]["model"], "kev-local")
        finally:
            os.environ.pop("SYSTEM_ONE_API_KEY", None)
            srv.shutdown()
            srv.server_close()


@unittest.skipUnless(have("torch") and have("transformers") and have("tokenizers"),
                     "torch/transformers não instalados")
class LocalLMTests(unittest.TestCase):
    """Cria um Qwen2 minúsculo e aleatório + tokenizer com chat template e testa o caminho todo."""

    @classmethod
    def setUpClass(cls):
        import torch
        from tokenizers import Tokenizer, models, pre_tokenizers, trainers, decoders
        from transformers import PreTrainedTokenizerFast, Qwen2Config, Qwen2ForCausalLM

        torch.manual_seed(0)
        cls.tmp = tempfile.mkdtemp(prefix="tiny-lm-")
        tok = Tokenizer(models.BPE(unk_token="<unk>"))
        tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
        tok.decoder = decoders.ByteLevel()
        corpus = [bridge.describe_state(bridge.parse_state(STATE_VISIBLE)), bridge.QUESTION,
                  " ".join(bridge.ACTION_DESCRIPTIONS.values()), "A B C D E F G H Answer: system user assistant"]
        trainer = trainers.BpeTrainer(vocab_size=400, special_tokens=["<unk>", "<|im_start|>", "<|im_end|>"],
                                      initial_alphabet=pre_tokenizers.ByteLevel.alphabet())
        tok.train_from_iterator(corpus, trainer)
        fast = PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="<unk>", eos_token="<|im_end|>")
        fast.chat_template = (
            "{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n{% endfor %}"
            "{% if add_generation_prompt %}<|im_start|>assistant\n"
            "{% if enable_thinking is defined and enable_thinking is false %}<think></think>{% endif %}{% endif %}"
        )
        fast.save_pretrained(cls.tmp)
        cfg = Qwen2Config(vocab_size=len(fast), hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                          num_attention_heads=2, num_key_value_heads=1, max_position_embeddings=2048)
        Qwen2ForCausalLM(cfg).save_pretrained(cls.tmp)

    def test_single_forward_letter_choice(self):
        d = bridge.LocalLMDecider(self.tmp, "cpu", "fp32")
        self.assertEqual(d.template_kwargs, {"enable_thinking": False})
        st = bridge.parse_state(STATE_VISIBLE)
        legal = bridge.legal_actions(st)
        prompt = d._prompt(st, legal)
        self.assertIn("<think></think>", prompt)  # enable_thinking=False chegou no template
        self.assertIn("A. hold:", prompt)

        calls = {"n": 0}
        orig = d.model.forward

        def counting(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        d.model.forward = counting
        dec = d.decide(st, legal)
        self.assertEqual(calls["n"], 1)  # uma forward pass, sem generate
        self.assertIn(dec.action, legal)
        self.assertEqual(list(dec.probs), legal)
        self.assertAlmostEqual(sum(dec.probs.values()), 1.0, places=4)


STATE_HEARING = ("S 900 64 100 22 60 ak47 10 20 0 hold 3000 16"
                 "|E 5 -1 -1.0 0 0 1400 back_left|H step back_left 11 600|H shot front 35 2200")


class HearingTests(unittest.TestCase):
    def test_parse_sounds_and_fair_enemy(self):
        st = bridge.parse_state(STATE_HEARING)
        e = st.enemies[0]
        self.assertIsNone(e.hp)
        self.assertIsNone(e.dist_m)
        self.assertEqual((e.seen_ago_ms, e.direction), (1400, "back_left"))
        self.assertEqual([(s.kind, s.direction, s.dist_m, s.ago_ms) for s in st.sounds],
                         [("step", "back_left", 11.0, 600), ("shot", "front", 35.0, 2200)])
        self.assertIsNone(st.nearest_visible)

    def test_old_enemy_format_still_parses(self):
        st = bridge.parse_state(STATE_VISIBLE)
        self.assertEqual(st.enemies[0].seen_ago_ms, 0)
        self.assertEqual(st.enemies[1].seen_ago_ms, bridge.NEVER_MS)

    def test_bad_sound_block(self):
        with self.assertRaises(bridge.ParseError):
            bridge.parse_state(STATE_HIDDEN + "|H step left")

    def test_describe_mentions_what_was_heard(self):
        text = bridge.describe_state(bridge.parse_state(STATE_HEARING))
        self.assertIn("You heard footsteps 0.6 s ago, about 11 m behind you on the left.", text)
        self.assertIn("last seen 1.4 s ago behind you on the left", text)
        self.assertNotIn("-1", text)  # nada de hp/distância escondidos vazando como -1
        self.assertIn("You hear nothing.", bridge.describe_state(bridge.parse_state(STATE_HIDDEN)))

    def test_face_sound_legality(self):
        self.assertIn("face_sound", bridge.legal_actions(bridge.parse_state(STATE_HEARING)))
        self.assertNotIn("face_sound", bridge.legal_actions(bridge.parse_state(STATE_HIDDEN)))
        self.assertNotIn("face_sound", bridge.legal_actions(bridge.parse_state(
            STATE_VISIBLE + "|H step left 5 100")))  # com inimigo visível, atira em vez de virar
        for s in (STATE_HEARING, STATE_VISIBLE, STATE_HIDDEN):
            self.assertLessEqual(len(bridge.legal_actions(bridge.parse_state(s))), len(bridge.LETTERS))

    def test_rules_turns_to_sound_and_walks(self):
        dec = bridge.RulesDecider().decide(bridge.parse_state(STATE_HEARING), bridge.legal_actions(
            bridge.parse_state(STATE_HEARING)))
        self.assertEqual((dec.action, dec.stance), ("face_sound", "walk"))

    def test_rule_stance(self):
        st = bridge.parse_state(STATE_HEARING)
        self.assertEqual(bridge.rule_stance(st, "hold"), "walk")
        self.assertEqual(bridge.rule_stance(st, "push"), "run")
        self.assertEqual(bridge.rule_stance(bridge.parse_state(STATE_VISIBLE), "hold"), "run")
        self.assertEqual(bridge.rule_stance(bridge.parse_state(STATE_HIDDEN), "hold"), "run")


class StanceQuestionTests(unittest.TestCase):
    """Postura como 2ª pergunta na mesma chamada (julia), e o fallback se o modelo recusar."""

    def _install_julia(self, accept_two: bool):
        calls = []

        class Engine:
            def predict(self, state, questions):
                calls.append(questions)
                if len(questions) > 1 and not accept_two:
                    raise ValueError("one question per call")
                out = {"action": {"choice": "face_sound",
                                  "probabilities": {k: 1 / len(questions["action"]["criteria"])
                                                    for k in questions["action"]["criteria"]}}}
                if "stance" in questions:
                    out["stance"] = {"choice": "walk", "probabilities": {"run": 0.2, "walk": 0.8}}
                return {"answers": out}

        mod = types.ModuleType("julia")
        mod.load_model = lambda path, device, **kw: Engine()
        sys.modules["julia"] = mod
        return calls

    def tearDown(self):
        sys.modules.pop("julia", None)

    def _decide(self, d):
        st = bridge.parse_state(STATE_HEARING)
        return d.decide(st, bridge.legal_actions(st))

    def test_two_questions_one_call(self):
        calls = self._install_julia(accept_two=True)
        d = bridge.JuliaDecider("Julia-1", "cpu", 1024)
        d.ask_stance = True
        dec = self._decide(d)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]), {"action", "stance"})
        self.assertEqual(list(calls[0]["stance"]["criteria"]), ["run", "walk"])
        self.assertEqual((dec.action, dec.stance, dec.stance_probs["walk"]), ("face_sound", "walk", 0.8))

    def test_falls_back_to_rule_if_model_rejects(self):
        calls = self._install_julia(accept_two=False)
        d = bridge.JuliaDecider("Julia-1", "cpu", 1024)
        d.ask_stance = True
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            dec = self._decide(d)
        self.assertFalse(d.ask_stance)            # desligou a 2ª pergunta
        self.assertEqual(len(calls), 2)            # tentou com 2, repetiu com 1
        self.assertEqual(dec.stance, "walk")       # regra: ouviu passo, ninguém visível
        dec = self._decide(d)
        self.assertEqual(len(calls), 3)            # daqui pra frente, 1 pergunta só

    def test_build_decider_stance_modes(self):
        self._install_julia(accept_two=True)
        base = dict(julia_path="Julia-1", julia_max_length=1024, seed=0)
        for decider, mode, expected in [("julia", "auto", True), ("rules", "auto", False),
                                        ("julia", "rule", False), ("rules", "model", True)]:
            args = Namespace(decider=decider, stance=mode, **base)
            self.assertEqual(bridge.build_decider(args, "cpu").ask_stance, expected, (decider, mode))


class LatencyTests(unittest.TestCase):
    """Item 1 do plano: travamento. Drenagem do socket, relógio de chegada e diagnóstico."""

    def test_parse_timing_fields(self):
        st = bridge.parse_state("S 5 100 0 30 90 ak47 1 2 3 push 99999 48 12 3600123 7")
        self.assertEqual((st.rtt_ms, st.net_ms, st.t_ms, st.stale_drops), (48, 12, 3600123, 7))
        st = bridge.parse_state("S 5 100 0 30 90 ak47 1 2 3 push 99999 -1 -1 3600123 0")
        self.assertEqual((st.rtt_ms, st.net_ms), (None, None))
        st = bridge.parse_state("S 5 100 0 30 90 ak47 1 2 3 push 99999 48")   # plugin antigo
        self.assertEqual((st.rtt_ms, st.t_ms), (48, None))

    def test_drain_keeps_only_newest(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            inbox = bridge.UdpInbox(rx)
            for i in range(25):
                tx.sendto(f"S {i}".encode(), rx.getsockname())
            time.sleep(0.05)
            self.assertTrue(inbox.wait(0.5))
            addr, data, _t, n = inbox.drain()
            self.assertEqual((data, n), (b"S 24", 25))
            self.assertEqual((inbox.dropped, inbox.max_burst), (24, 25))
            self.assertIsNone(inbox.drain())             # nada mais: não bloqueia
            self.assertFalse(inbox.wait(0.01))
        finally:
            rx.close()
            tx.close()

    def test_arrival_clock(self):
        c = bridge.ArrivalClock(window_s=60)
        # relógios com diferença fixa de 5000 ms e 1 ms de rede: atraso extra ~0
        for i in range(10):
            self.assertAlmostEqual(c.observe(1000 + i * 31, (5001 + 1000 + i * 31) / 1000), 0.0, places=6)
        # agora o estado chega 300 ms atrasado
        self.assertAlmostEqual(c.observe(2000, (5001 + 2000 + 300) / 1000), 300.0, places=6)
        # servidor reiniciou (relógio voltou): recomeça sem acusar atraso gigante
        self.assertAlmostEqual(c.observe(10, 100.0), 0.0, places=6)
        self.assertIsNone(c.observe(None, 1.0))

    def test_diagnosis_by_leg(self):
        def stats(arrival, bridge_ms, net, rtt):
            s = bridge.Stats()
            s.arrival_ms, s.bridge_ms, s.net_ms, s.rtt_ms = arrival, bridge_ms, net, rtt
            return s
        ok = [1.0] * 20
        self.assertIsNone(stats(ok, [30.0] * 20, ok, [40.0] * 20).diagnose())
        self.assertIn("CHEGADA", stats([400.0] * 20, [30.0] * 20, [401.0] * 20, [430.0] * 20).diagnose())
        self.assertIn("DENTRO", stats(ok, [300.0] * 20, ok, [310.0] * 20).diagnose())
        self.assertIn("VOLTA", stats(ok, [30.0] * 20, [500.0] * 20, [530.0] * 20).diagnose())
        self.assertIn("VOLTA", stats(ok, [30.0] * 20, ok, [40.0] * 20).diagnose(stale_drops=12))

    def test_default_has_no_rate_limit(self):
        import contextlib
        import io
        # --max-hz abaixo da taxa de estados adiciona atraso; o padrão agora é sem limite
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                bridge.main(["--help"])
        src = open(os.path.join(HERE, "bridge.py"), encoding="utf-8").read()
        self.assertIn('"--max-hz", type=float, default=0.0', src)


class OverlayTests(unittest.TestCase):
    def test_serves_page_and_state(self):
        import urllib.request
        ov = bridge.OverlayServer("127.0.0.1", 0)
        try:
            base = f"http://127.0.0.1:{ov.port}"
            html = urllib.request.urlopen(base + "/?scale=1.2").read().decode()
            self.assertIn("<title>Julia</title>", html)
            self.assertEqual(json.loads(urllib.request.urlopen(base + "/state").read()), {})
            st = bridge.parse_state(STATE_HEARING)
            ov.update(st, bridge.Decision("face_sound", {"face_sound": 0.6, "hold": 0.4}, "walk"), 27.6,
                      "julia", "cuda")
            d = json.loads(urllib.request.urlopen(base + "/state").read())
            self.assertEqual((d["action"], d["stance"], d["device"]), ("face_sound", "walk", "cuda"))
            self.assertEqual(d["situation"], "heard footsteps back-left ~11m")   # em inglês: é o que o público vê
        finally:
            ov.close()

    def test_situation_en(self):
        self.assertEqual(bridge.situation_en(bridge.parse_state(STATE_VISIBLE)), "enemy 18m · AIMING AT HER")
        self.assertEqual(bridge.situation_en(bridge.parse_state(STATE_HIDDEN)), "no contact")


class EndToEndTests(unittest.TestCase):
    def test_bridge_and_fake_plugin(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
        br = subprocess.Popen([sys.executable, os.path.join(HERE, "bridge.py"), "--decider", "rules",
                               "--port", str(port), "--log-jsonl", log, "--quiet"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.0)
            fp = subprocess.run([sys.executable, os.path.join(HERE, "fake_plugin.py"), "--port", str(port),
                                 "--duration", "3"], capture_output=True, text=True, timeout=30)
            self.assertEqual(fp.returncode, 0, fp.stdout + fp.stderr)
            self.assertIn("respostas", fp.stdout)
        finally:
            br.terminate()
            out, _ = br.communicate(timeout=10)
        self.assertIn("saindo", out)  # SIGTERM saiu limpo
        with open(log) as f:
            rows = [json.loads(line) for line in f]
        self.assertGreater(len(rows), 30)
        self.assertTrue(all(r["action"] in r["legal"] for r in rows))
        self.assertTrue(all(r["stance"] in ("run", "walk") for r in rows))
        self.assertTrue(any(r["action"] == "face_sound" for r in rows))  # o fake faz barulho de passo
        self.assertIn("posturas", fp.stdout)
        # protocolo novo: o bridge mede chegada e o plugin (fake) devolve rtt/rede
        self.assertTrue(any(r["arrival_ms"] is not None for r in rows))
        self.assertTrue(any(r["net_ms"] is not None for r in rows))
        self.assertIn("descartadas velhas: 0", fp.stdout)
        os.unlink(log)


if __name__ == "__main__":
    unittest.main()

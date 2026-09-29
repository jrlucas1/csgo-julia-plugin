# jev-bot

A CS:GO Legacy bot controlled by a **System One**-style decision model: it receives the game state and a list of options, then chooses one. It runs on your own network. **Use only on a local server with `-insecure`, against people who have agreed to play. Never use it in matchmaking or on public servers.**

```
 ┌──────────── Desktop (Windows) ────────────┐        ┌──── Desktop or MacBook ────┐
 │ CS:GO client  ◄──►  srcds + SourceMod     │  UDP   │ bridge.py                  │
 │                       jev_bot.smx (body)  │ ─────► │ rules / julia / laya /      │
 │   aim, fire, reaction, reload, spots      │ ◄───── │ local / http (brain)        │
 └───────────────────────────────────────────┘ 27500  └────────────────────────────┘
      S tick hp ... |E ...  ──►            ◄──  A <action> <tick>
```

The model chooses the **tactic** for the next 0.35 seconds (`hold`, `peek_left`, `peek_right`, `strafe_shoot`, `crouch_spray`, `fall_back`, `push`, `reload`, `face_sound`) and a **stance** (`run` or `walk` silently). Aim and firing stay in the plugin; cvars control difficulty.

The bot **hears** enemy footsteps, gunfire, reloads, and jumps. It has **no wallhack**: for enemies it cannot see, it only knows what it heard and where it last saw them.

## Contents

| File | Description |
|---|---|
| `csgo/addons/sourcemod/scripting/jev_bot.sp` | SourcePawn plugin (newdecls, SourceMod 1.10+) |
| `csgo/addons/sourcemod/plugins/jev_bot.smx` | Precompiled plugin (spcomp 1.11), if you want to skip compilation |
| `csgo/cfg/jev_duel.cfg` | 1v1 setup: you as CT vs. bot as T, AK, no warmup, buying, or C4 |
| `csgo/cfg/jev_justo.cfg`, `jev_demonio.cfg`, `jev_facil.cfg` | Difficulty presets |
| `bridge/bridge.py` | Decision bridge (UDP, decision backends, and metrics) |
| `bridge/fake_plugin.py` | Simulates the plugin so you can test the bridge without launching the game |
| `bridge/test_bridge.py` | Parser, fake-model decision, and end-to-end tests |
| `bridge/requirements*.txt` | Standard-library base plus one requirements file per backend |

The Julia model files and weights are downloaded separately by the setup script; they are not stored in this repository.

### Verification notes

- `jev_bot.sp` compiled without warnings with spcomp 1.11 and both SourceMod 1.11 and 1.10 includes. The `.smx` uses only `SocketCreate`, `SocketConnect`, and `SocketSend`, available in both the JoinedSenses build and classic 3.0.1 build.
- The sm-ext-socket UDP behavior was checked against the extension source. Receiving begins only after the asynchronous `Connect`. A closed port produces a receive error on Linux and a disconnect on Windows; the plugin handles both.
- `test_bridge.py`: 26 tests passed. The `rules` bridge handled 160/160 `fake_plugin` states with about 0.4 ms round-trip time. With a tiny random LM (`local`, CPU), a decision took about 5 ms.
- **Not verified here (requires the game and model weights):** the plugin in-game and Julia/Laya with their actual models. The adapters were checked with fake models that return the documented format.

---

## 1. Install the game and server

### 1.1 Client

In Steam: Library → Counter-Strike 2 → Properties → **Betas** → `csgo_legacy`.
Launch options: `-insecure -novid -console`.

> Community guides also mention a standalone **CS:GO Legacy** app (AppID 4465480). Those guides say that if you or a friend joins through that app, the server needs the **CSGOEngineFix** plugin. It is not needed when using the `csgo_legacy` beta in CS2. This has not been tested here.

### 1.2 Dedicated server (SteamCMD, app 740)

1. Download SteamCMD for Windows (linked from the Valve Developer Wiki's *SteamCMD* page) and extract it to `C:\steamcmd`.
2. Install the server:
   ```bat
   C:\steamcmd\steamcmd.exe +force_install_dir C:\csgo-ds +login anonymous +app_update 740 validate +quit
   ```
   This should create `C:\csgo-ds\srcds.exe` and `C:\csgo-ds\csgo`. Current community guides say app 740 downloads the CS:GO Legacy server. If it downloads something else, try adding `-beta csgo_legacy` to `app_update`.
3. Create `C:\csgo-ds\start_jev.bat`:
   ```bat
   @echo off
   cd /d C:\csgo-ds
   srcds.exe -game csgo -console -insecure -tickrate 64 +sv_lan 1 +game_type 0 +game_mode 1 +map de_dust2 +maxplayers 10
   ```
   For more responsiveness, use `-tickrate 128` (see Latency). On a LAN with `-insecure`, you do not need a GSLT.

### 1.3 MetaMod:Source and SourceMod

Download the **stable Windows builds** from *metamodsource.net* and *sourcemod.net*. Both 1.11 releases still support CS:GO. Extract both archives **inside `C:\csgo-ds\csgo`**, so you get `csgo\addons\metamod\...` and `csgo\addons\sourcemod\...`.

Check the server console with `meta version` and `sm version`.

**Admin access (for `sm_jev`):** add your SteamID to `csgo\addons\sourcemod\configs\admins_simple.ini`:
```
"STEAM_1:1:12345678"  "99:z"
```
Replace the example ID with your own; `status` in the console shows it.

### 1.4 sm-ext-socket

From the **JoinedSenses/sm-ext-socket** repository's Releases:
- `socket.ext.dll` (release "0.1 – Windows only build"; check the 0.2 assets as well) → `csgo\addons\sourcemod\extensions\`
- `scripting/include/socket.inc` (from the repository) → `csgo\addons\sourcemod\scripting\include\`

Run `sm exts list` and confirm that "Socket" appears.

> The precompiled `.smx` also works with the classic 3.0.1 build from the AlliedModders forum's "Socket (3.0.1)" thread because it only uses natives supported by both versions. To **compile**, use JoinedSenses' `socket.inc`.

## 2. Compile and install the plugin

Copy `csgo/addons/...` and `csgo/cfg/...` from this repository into `C:\csgo-ds\csgo`. Then:
```bat
cd C:\csgo-ds\csgo\addons\sourcemod\scripting
spcomp.exe jev_bot.sp -o..\plugins\jev_bot.smx
```
Or use the included `jev_bot.smx`. Restart the server or run `sm plugins load jev_bot`.

The first time it runs, SourceMod creates `csgo\cfg\sourcemod\jev_bot.cfg` with all cvars (AutoExecConfig). Set `jev_bridge_host` and related settings there.

## 3. Start everything

1. Run `start_jev.bat`.
2. In the server console, run `exec jev_duel`, then `exec jev_justo`.
3. On the client, connect with `connect 127.0.0.1` (friends on the LAN: `connect <desktop IP>`).
4. Set up the duel wherever you want:
   - Go to the bot's position, aim where it should look, then run `sm_jev_spot bot`.
   - Go to your position, then run `sm_jev_spot human`.
   - Run `mp_restartgame 1`. Spots are saved per map in `addons\sourcemod\data\jev_bot_spots.txt`.
   - To clear them, run `sm_jev_spot clear`.

## 4. Test the plugin without the bridge

With the bridge **stopped**:
```
sm_jev peek_left 2
sm_jev crouch_spray 3
sm_jev push 1.5
sm_jev peek_right 2 walk   // peek while walking (shift, silently)
sm_jev face_sound 2        // turn toward the most recent sound
jev_debug 1                // show sent states and heard sounds ("[jev] heard step from ...")
```
While an `sm_jev` action is active, the bridge is ignored. If the bridge is stopped, the log reports `bridge ... unavailable` once and retries every 2 seconds without spamming.

## 5. Run the bridge

Python 3.11 or newer is required.
```bat
cd bridge
python -m venv .venv && .venv\Scripts\activate          (macOS: source .venv/bin/activate)
python test_bridge.py                                    (optional)
```

### 5.1 Start with `rules`

```bat
python bridge.py --decider rules
python fake_plugin.py            (in another terminal: test without the game)
```
Then join the game. The bridge prints each action change and latency statistics every 5 seconds.

### 5.2 Models

**NVIDIA GPU on Windows:** install the CUDA-enabled PyTorch build **before** the extras (the PyPI build for Windows is CPU-only). Use the selector at *pytorch.org/get-started* and check with `python -c "import torch; print(torch.cuda.is_available())"`.

| Backend | Install | Command |
|---|---|---|
| local (Qwen, etc.) | `pip install -r requirements-local.txt` | `python bridge.py --decider local --model Qwen/Qwen2.5-3B-Instruct` |
| laya | `pip install -r requirements-laya.txt` | `python bridge.py --decider laya` (`--laya-subfolder multilingual` for the multilingual checkpoint) |
| julia | `pip install -r requirements-julia.txt`, then run the setup script to download the model | `python bridge.py --decider julia --julia-path Julia-1` |
| http (TypeSafe) | `pip install -r requirements-http.txt`, set `TYPESAFE_API_KEY` | `python bridge.py --decider http --http-backend typesafe` |
| http (local server) | none | `python bridge.py --decider http --http-url http://127.0.0.1:8000/...` |

Notes:
- **Julia 1:** The API (`from julia import load_model`, `engine.predict(state=, questions=)` with `{"type":"choice","instructions","criteria"}`) was checked against the model card on 2026-09-28. The card documents `cpu` and `cuda`; if `mps` fails, the bridge falls back to CPU. The default is `--julia-max-length 1024`, matching the published benchmarks. The package is named `julia`, like PyJulia, so use a separate virtual environment. Julia's downloaded model files are excluded from this repository and fetched by `setup_julia.bat` / `setup_julia.sh`.
- **Laya:** The `laya.load(model, device=..., subfolder=...)` interface was checked against semantic-operators 0.11.0 and laya 0.3.21; it accepts `cuda`, `mps`, and `cpu`.
- **local:** Options are labeled A–H. It uses one forward pass and picks the highest logit for the `A` / ` A` tokens without generating text. It uses `enable_thinking=False` when the chat template supports that variable. On Mac, use `--dtype fp16` if bf16 fails.
- **http raw is a placeholder.** The endpoint and schema for Kev/OpenJev are not known. The request follows the Julia/Laya question format and expects `{"answers":{"action":{"choice","probabilities"}}}` in response. Adjust `_raw_request` / `_raw_parse` in `bridge.py` to match your server's README. The `typesafe` backend uses the actual SDK, but it is **not local**.

### 5.3 Useful options

```
--device auto|cuda|mps|cpu   --max-hz 0           --warmup 3      --rcvbuf 16384
--log-jsonl game.jsonl       --quiet              --commit-ms 0   (hold an action for a minimum time)
--overlay-file brain.txt      (text output for OBS; see Recording)
--stance auto|model|rule      (auto asks julia/laya/http; rules/local use a rule)
```

### 5.4 Hearing and stance

The plugin listens to game events (`player_footstep`, `weapon_fire`, `weapon_reload`, `player_jump`) from enemies only and within a limited range:

| Sound | Approx. range |
|---|---|
| Footstep | ~28 m (1100 units) |
| Gunfire | ~76 m (3000 units); ~23 m with a silencer |
| Reload | ~15 m |
| Jump | ~20 m |

- **Silent movement:** the game does not emit `player_footstep` when someone walks with Shift or crouches, so this applies equally to the bot and the player.
- **Approximate position:** sound positions include up to 8% distance error to avoid acting like radar.
- **Direction:** relative to the bot's view, in 8 slices (`front`, `front_left`, `left`, `back_left`, `back`, etc.).
- **Model input:** for example, *"You heard footsteps 0.6 s ago, about 11 m behind you on the left."*

`face_sound` turns toward and holds aim on the most recent sound, or the last seen position if it is more recent.

**Stance** is sent as a second question in the same Julia/Laya/http call ("run or walk silently?"). If a model rejects two questions in one call, the bridge warns once and switches to a rule: walk silently when an enemy is nearby but unseen, and run to push, retreat, or reload. `rules` and `local` always use a rule for stance (`local` would need a second forward pass otherwise, doubling latency).

Protocol:
```
S tick hp armor clip reserve weapon x y z action hurt_ago_ms rtt_ms
  |E userid hp dist_m visible aiming seen_ago_ms direction  (hp/dist = -1 when not visible)
  |H type direction dist_m ago_ms                           (sounds from the last 3 s)
A <action> <tick> <run|walk>
```

## 6. Two machines (bridge on a MacBook)

1. **Reserve a fixed Mac IP:** create a DHCP reservation in your router for the Mac's MAC address.
   - Ethernet and Wi-Fi have different MAC addresses; reserve the one you will use.
   - On macOS Wi-Fi, turn off or pin **Private Wi-Fi Address** for that network (Settings → Wi-Fi → network details), or the MAC may change and the reservation will stop matching.
2. **On the Mac:**
   ```
   python bridge.py --decider laya --bind 0.0.0.0 --device mps
   ```
   **macOS firewall:** it is app-based, not port-based. The first time, allow incoming connections for Python. If no prompt appears, open Settings → Network → Firewall → Options, click `+`, and add the actual Python binary. Find it with `python -c "import os,sys;print(os.path.realpath(sys.executable))"` (the venv path is a symlink).
3. **On Windows:** in `csgo\cfg\sourcemod\jev_bot.cfg`, set `jev_bridge_host "<MAC IP>"`. The cvar reconnects automatically, so you can change it while running.
   - srcds sends UDP **to** the Mac and the Windows firewall allows the response, so a Windows firewall rule is usually unnecessary.
   - To test in the opposite direction, with the bridge on Windows and `fake_plugin` on the Mac, open the port in an elevated PowerShell:
     ```powershell
     New-NetFirewallRule -DisplayName "jev bridge UDP 27500" -Direction Inbound -Protocol UDP -LocalPort 27500 -Action Allow -Profile Private
     ```
     The network must be set to **Private**. Friends on the LAN need inbound access on port 27015 for `srcds.exe`; Windows prompts the first time.
4. **Test without the game, on Windows:** `python fake_plugin.py --host <MAC IP>`.
5. **Ethernet is better than Wi-Fi.** On Mac, Wi-Fi can have periodic 50–100 ms spikes from power saving, scans, and AirDrop/Handoff/Sidecar AWDL. Use a USB-C-to-Ethernet adapter. If you must use Wi-Fi, turn off AirDrop and Handoff while playing.

**Compare desktop vs. Mac and Ethernet vs. Wi-Fi:** run `python fake_plugin.py --host <ip> --duration 60` for all four combinations and compare p50/p95. In-game, check the `total(rtt)` column printed by the bridge.

## 7. Latency log

```
[julia] 15.1 dec/s | arrival p50 0.4 p95 0.9 | model p50 27.5 p95 28.3 | bridge p50 28.1 p95 29.0 | network p50 1.0 p95 2.0 | total(rtt) p50 30.0 p95 33.0 max 41 ms | dropped 16 (max burst 1) | stale at plugin 0 | errors 0
```

- **arrival:** extra time for a state to travel from the server to the bridge. The plugin sends `GetEngineTime` with each state; the bridge compares it to its clock. The lowest value over the last 60 seconds becomes the baseline. If it rises, the delay is on the way in (plugin send, network, or socket buffer).
- **model:** time spent in the decision backend.
- **bridge:** time from reading the state to sending the response. The bridge drains the socket and keeps only the newest state, so this is mostly model time. States waiting in the kernel buffer show up under **arrival**.
- **network:** RTT minus the time reported by the bridge. It includes network round-trip time and the server callback wait; the socket extension delivers only one callback per server frame.
- **total(rtt):** measured by the plugin with `GetEngineTime`, below one-tick precision. It reports the largest RTT since the previous state so a backlog of delayed responses is not hidden. `max` is the worst in the window. The engine clock is float32, so precision drops to about 1 ms after the server has been running for roughly 3 hours.
- **dropped / max burst:** states overwritten by a newer state before processing. A high max burst (dozens) means a queue formed in the socket.
- **stale at plugin:** responses discarded because their source state exceeded `jev_max_cmd_age` ticks (default 16). This makes the bot hold instead of acting on old information. Set `jev_max_cmd_age 0` to disable.
- **`DELAY: ...`:** when total latency exceeds 100 ms, or the plugin discards stale commands, the bridge identifies the largest delay segment: arrival, inside the bridge, or return/server.

Test diagnostics without the game:
```
python fake_plugin.py --tickrate 128 --hz 32 --delay-send 400 3 3   # inbound delay: bridge reports ARRIVAL
python fake_plugin.py --tickrate 128 --hz 32 --pause-rx 3 3         # return delay: bridge reports RETURN
```

Tips:
- Max state age is `jev_state_every / tickrate`: 4/64 = 62 ms. `jev_state_every 2` with `-tickrate 128` is 16 ms.
- Keep `--max-hz` at 0 (the default). A cap below the state rate makes the bridge sleep and decide on an old state, **adding** delay.
- `--rcvbuf 16384` (default) keeps the receive buffer small. If something stalls, the kernel drops states instead of holding seconds of them.
- Start with small models. For `local`, a KV cache for the fixed prefix (system prompt + descriptions) is a possible optimization.
- Windows: use the "High performance" power plan. Mac: stay plugged in and turn off Low Power Mode.

## 8. Difficulty

| Cvar | Effect | Easy | **Fair** | **Demon** |
|---|---|---:|---:|---:|
| `jev_reaction_ms` | time between seeing and being able to fire | 420 | 260 | 120 |
| `jev_aim_jitter` | aim error (degrees) | 3.0 | 1.8 | 0.3 |
| `jev_aim_speed` | max turn speed (degrees/tick) | 3 | 5 | 20 |
| `jev_fire_cone` | only fire when aim is within this angle (degrees) | 3.5 | 2.5 | 1.5 |
| `jev_recoil_control` | recoil compensation (0–1) | 0.2 | 0.5 | 1.0 |
| `jev_state_every` | ticks between states | 4 | 4 | 2 |

Run `exec jev_facil`, `exec jev_justo`, or `exec jev_demonio`. Other cvars include `jev_enabled`, `jev_move_speed` (0–450; the weapon caps it), `jev_debug`, `jev_hud`, and hearing settings:
- `jev_hearing 1`: enable hearing.
- `jev_hear_scale 1.0`: multiply hearing range (0.5 = half-deaf, 2 = bat hearing).
- `jev_fair_info 1`: hide HP/distance for invisible enemies. Set to 0 to restore "wallhack" information for comparisons.

"Fair" is tuned around a good human reference (~250 ms visual reaction, non-instant aim). Demon mode **will** feel like a cheat; that is the point.

## 9. Record video 🎬

You can record in three layers.

**a) Server demo (SourceTV): records everything from all angles.** Before loading the map (or run `changelevel` afterward), use the server console:
```
tv_enable 1
tv_delay 0
tv_autorecord 0
changelevel de_dust2
exec jev_duel
tv_record jev_duel_01      // start
tv_stoprecord               // stop
```
The demo is saved to `C:\csgo-ds\csgo\jev_duel_01.dem`. Copy it to the client's `csgo` folder and run `playdemo jev_duel_01`. Use Shift+F2 (`demoui`) to control speed and skip rounds. Use `spec_player` / `spec_next` to view the **bot's** camera; its aim makes the demon-like reflexes easy to see. SourceTV counts as an extra "player"; the plugin already ignores it.

**b) Client demo:** `record jev_pov` / `stop`. It records only your point of view, which is useful for a reaction video.

**c) Julia panel in OBS:**

The bridge serves an English-language overlay showing the current action, animated probability bars, stance ("walking silently"), what Julia sees or hears ("heard footsteps left ~12m", "enemy 22m · AIMING AT HER"), HP, ammo, and latency. The panel flashes when her decision changes.

1. `run_julia.sh` / `run_julia.bat` starts the panel on port **27501** and prints its URL. You can also set `--overlay-port 27501` manually.
2. In OBS, choose **Sources → + → Browser**.
   - URL: `http://127.0.0.1:27501/` (bridge on this PC) or `http://<MAC IP>:27501/` (bridge on the Mac).
   - Width **1920**, height **1080**. The background is transparent.
3. URL options:
   - `?pos=br`: corner; `tl`, `tr`, `bl` (default), or `br`.
   - `?scale=1.3`: panel size.
   - `?bg=1`: dark background, useful for previewing in a browser.
4. If the bridge stops, the green dot next to "JULIA" turns red.

Extras:
- `jev_hud 1` shows `JULIA: crouch_spray | 64 HP | rtt 16 ms` in the center of the screen whenever Julia changes action. This is also recorded in demos.
- `--overlay-file brain.txt` writes plain text for an OBS **Text (GDI+)** source set to "Read from file".

Video ideas:
- **Human vs. rules vs. Julia vs. Laya:** same spots, 10 rounds each, with the score on screen. `--log-jsonl` provides the data.
- **Demon mode vs. the most confident friend,** with the brain cam showing `AIMING!` half a second before they die.
- **Slow motion (`demo_timescale 0.25`)** on a peek, with probabilities changing frame by frame.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `sm plugins list` reports `Native "SocketCreate" was not found` | Extension did not load → run `sm exts list` and check the `.dll` in `extensions\` |
| `errors_*.log`: `bridge ... unavailable` | Bridge is stopped, IP/port is wrong, or firewall blocks it. Run `fake_plugin` against the same host |
| Bridge prints nothing | `jev_debug 1` shows whether the plugin is sending. Check `jev_bridge_host` and `--bind` (`0.0.0.0` for another machine) |
| Bot is idle / behaves like a normal bot | Is `jev_enabled 1` set? Is there a bot (`bot_quota 1`)? Try `sm_jev push 2` |
| Bot does not fire | `jev_fire_cone` is too low with high `jev_aim_jitter`, or `jev_reaction_ms` is too high |
| Spots do not apply | They apply on the next spawn only: `mp_restartgame 1` |
| `ConnectionResetError` on Windows | Already handled (SIO_UDP_CONNRESET disabled and exception ignored) |
| `total(rtt)` is always 0 | Normal on the same machine (one-tick resolution) |
| Bot never uses `face_sound` / no "heard" messages in `jev_debug 1` | Check `jev_hearing 1`; see if SourceMod logs a missing event; increase `jev_hear_scale` |
| Julia: `ImportError` / no `load_model` | PyJulia may be in the same venv, or `pip install -e ./Julia-1` may not have run |
| Mac `mps` fails with bf16 | Use `--dtype fp16` (local), or update macOS / PyTorch |

## 11. Known limitations

- **Sees through smoke:** the visibility trace ignores smoke. Flashbangs do not affect the bot either.
- **No navigation:** actions only move relative to the bot's view (left/right/forward/back). It may walk into walls or off ledges. Use spots to position it.
- **Simplified hearing:** fixed range by sound type; walls do not muffle it and floor type does not matter. Ranges are approximate. Use `jev_debug 1` to check that all four events arrive; if an event is unavailable in the game build, the plugin logs it once at startup.
- **Aims at eye position** (about head height), without accounting for hitbox animation.
- **One bot only;** all humans share the `human` spot.
- **Shared CS:GO classnames** (cz75a/p250, usp/p2000, mp5sd/mp7, revolver/deagle) can confuse automatic vs. semi-auto detection. This does not matter for AK.
- Each new action replaces the previous one. A `peek` chosen once lasts about one decision (~60 ms). `rules` holds a peek for 0.45 seconds; use `--commit-ms` for models.

## 12. Future work

**a) Named point graph on Dust2**
- `sm_jev_point add long_doors` / `link long_doors long_corner` to save points (position + hold angle) and edges to `data/jev_graph_de_dust2.txt`. Alternatively, read the documented `.nav` map format and name the areas.
- Add a path follower to the plugin: yaw toward the next point → `vel[0]`, with arrival by radius.
- Add System One actions: `advance` (go to the next planned point), `hold_angle` (hold the point's saved angle), `retreat_to <previous point>`. Keep the legal action list at 20 or fewer so Julia and Laya still work.
- Add `current_point`, `next_point`, and threat angle to the state.

**b) Per-round LLM planner over the fast model**
- At `round_start` (freeze time), a larger LLM (local Qwen 7B or Claude via API) receives the score, money, recent round history (from JSONL: where the human appeared and how the bot died), and the graph. It returns a **plan**, for example `{"route": ["t_spawn","outside_long","long_doors","long_corner"], "stance": {"long_corner": "hold_angle"}, "aggression": 0.7}`.
- The bridge stores the plan and turns it into **context** for System One ("plan: hold long corner; high aggression") and constraints on legal actions.
- Two clocks: the planner runs once per round (seconds are fine); System One runs at 16–32 Hz (milliseconds). This follows the pattern "System Two plans, System One acts".
- With accumulated JSONL, Julia or Laya could be tuned on your own matches (label winning rounds as good examples) and compared with `rules` using the scoreboard.
"# csgo-julia-plugin" 

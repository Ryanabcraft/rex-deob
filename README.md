# deobf bot

Discord bot + CLI that auto-detects a Roblox obfuscator and deobfuscates the
script. Drop a file or link, get readable Luau back, plus a risk scan of what
the script was hiding.

## What is in this repo

Everything needed to run it: the orchestration (detection, engine adapters, the
job pipeline, the indicator analyzer, the Discord and CLI front ends, the test
harness) **and** the engine trees in `engines/` plus the bundled binaries in
`tools/`. Clone, run `install.ps1`, paste a token, and the whole chain works.

Each `engines/*` folder comes from its author and keeps its original license
inside the tree.

## Commands

| command | what it does |
|---|---|
| `.l` | attachment, reply-to-file, raw link, or pasted code block — detects and deobfuscates |
| `.get <url>` | downloads the script from the link and deobfuscates |
| `.trace` | behaviour trace instead of full devirtualization (Luraph 14/15, MoonSec) |
| `.detect` | fingerprint only, no engine run |
| `.engines` | engine list + availability |
| `.help` | command card |

Every `.l` run returns `script.deobf.lua`, `script.analysis.txt` (webhooks,
HttpGet, loadstring, fs access…), plus the engine's strings report when it has
one. Zip uploads are unpacked and the biggest Lua member is used.

## Engines

| engine | covers | runtime |
|---|---|---|
| `bytecode` | raw `.luac` / Luau bytecode → source (Rust luadec) | bundled exe |
| `luraph15` | Luraph v15 / v15.1, devirtualization + polish | node + bundled luau |
| `ib1` | IronBrew1 static payload → IR + all constants | python |
| `luraph14` | Luraph v14, generic VM traces | python + bundled luau |
| `ib2net` | IronBrew 2 devirtualizer (LuaAnalysis) | prebuilt .NET |
| `ironbrew2` | IronBrew 2.7.0–2.7.2 / AztupBrew (fallback) | python (luaparser) |
| `prometheus` | Prometheus / WeAreDevs (pdeobf → wad dumper → promjs → leakd) | node + python |
| `luaobfuscator` | LuaObfuscator Chaotic Good/Evil | node |
| `moonsec` | MoonSec V3 → bytecode → decompile (luadec → unluac → lua5.1) | .NET 8 + java |
| `orrxl4` | Orrxl4 loader peel + interpret | python |
| `synapsexen` | Synapse Xen 1.1.2 (partial: VM program decoded) | python + lua5.1 |
| `psu` | PSU 4.0 / 4.5 → Lua 5.1 chunk → luadec (structured source) | node + luadec |
| `hercules` | Hercules 1.6.x–2.0.x static decompile | python |
| `moonveil` | MoonVeil 2.1 analysis/descramble (tiered output) | python |
| `ironveil` | IronVeil v1 | node |
| `f77` | 77fuscator 0.4–0.6 → Lua 5.1 chunk → luadec | .NET + luadec |
| `wyn` | wYnFuscate (dynamic run in bundled Luau) | python + luau |
| `ib3` | IronBrew3 static payload → constants + IR (partial) | python |
| `centurion` | Centurion v6 / v6.1 (offline, partial when gated) | python + luau tools |
| `luarmor` | Luarmor loader stubs (probe / fetch) | python |
| `unveilr` | universal fallback: executor-env trace in Lune, effects rendered back to Luau | lune + luau |
| `strings` | last resort, family-agnostic: literals, URLs, decoded payloads (no execution) | python |

Engines only run when their fingerprint matches (or when forced with
`--engine`), so a mystery script does not get garbage traces from the wrong
VM. The two detectors that run on every input — Prometheus `pdeobf` and
LuaObfuscator — are sub-second.

Families without an engine — PSU v5.0A (deobf covers 4.x), Boronide, LPS, and
MoonVeil 1.x — fall through to the `unveilr` universal trace, which renders
what the script did instead of source. If even that fails, `strings` returns
the literals, URLs and decoded payloads the script carries, so an unknown
obfuscator never produces an empty reply. The reply always names the family.

## Tests

`tests/corpus/` holds labelled samples per family and `tests/run_corpus.py`
runs the whole matrix. `tests/run_terrorlua.py` pulls real samples from
`terrorlua/obfuscator-samples` (3 per family, largest under the size cap) and
measures the engines, skipping the universal fallbacks:

```powershell
.\.venv\Scripts\python.exe tests\run_corpus.py --write
.\.venv\Scripts\python.exe tests\run_corpus.py -f wearedevs
.\.venv\Scripts\python.exe tests\run_terrorlua.py --per-family 3 --max-kb 200
```

## Install (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
copy .env.example .env   # paste DISCORD_TOKEN
.\.venv\Scripts\python.exe bot.py
```

The script builds a Python 3.13 venv, installs `requirements.txt`, and prints
which engines are ready. Necessary runtimes: **Python 3.10+**, **Node 18+**,
**.NET 8 runtime**, **Java 8+** (or the bundled lua5.1 decompile path).
Luau binaries ship in `tools/` and inside each engine tree.

## CLI

```powershell
.\.venv\Scripts\python.exe cli.py script.lua
.\.venv\Scripts\python.exe cli.py script.lua --trace
.\.venv\Scripts\python.exe cli.py https://host/loader.lua --get
.\.venv\Scripts\python.exe cli.py script.lua --engine luraph15 --out out\
.\.venv\Scripts\python.exe cli.py --engines
```

## Environment

| var | default | |
|---|---|---|
| `DISCORD_TOKEN` | — | bot token |
| `PREFIX` | `.` | command prefix |
| `MAX_FILE_MB` | 10 | upload/download cap |
| `MAX_PARALLEL` | 2 | concurrent jobs |
| `COOLDOWN_S` | 8 | per-user cooldown |
| `JOB_TIMEOUT_S` | 1200 | whole-job cap |
| `KEEP_JOBS` | 0 | `0` deletes the job folder after the reply is posted |
| `TIMEOUT_LURAPH15` | 900 | per-engine budgets (`TIMEOUT_*`) |

## Layout

```
bot.py              Discord front end
cli.py              same pipeline without Discord
core/               detection, adapters, pipeline, analyzer, downloader
engines/luraph15/   Luraph v15 / v15.1 deobfuscator
engines/luraph14/   Luraph v14 / IronBrew engine
engines/prometheus/ Prometheus / WeAreDevs (pdeobf)
engines/moonsec/    MoonSec V3 deobfuscator + unluac + lua5.1
engines/centurion/  Centurion v6 / v6.1 research deobfuscator
engines/luarmor/    Luarmor loader probe/fetch
tools/              luau, luau-ast, luau-compile, luadec (Windows)
tests/              labelled corpus + regression harness
work/               per-job artifacts (input, output, logs, reports)
```

## Notes

- Jobs run as subprocesses with hard timeouts; a hung engine is killed with its
  process tree and the next engine in the chain is tried.
- Luraph v15 is slow by design (sandbox trace + devirtualization): expect
  seconds for small hubs and a few minutes for 500 KB scripts.
- MoonSec returns both `deobf.lua` and `deobf.luac` when the decompiler runs.
- Centurion may report *partial* when a script needs live anti-tamper answers;
  the lift is still sent alongside the trace.
- Use it on code you own or are allowed to review. It exists to show what a
  protected script does before someone runs it.

Engine sources come from their authors and keep their original licenses inside
each `engines/*` directory.

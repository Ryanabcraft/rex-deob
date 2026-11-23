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
| `.full` | same as `.l`, plus `strings.txt`, `analysis.txt` and `trace.txt` |
| `.trace` | behaviour trace instead of full devirtualization (Luraph 14/15, MoonSec) |
| `.detect` | fingerprint only, no engine run |
| `.engines` | engine list + availability |
| `.help` | command card |

Every run returns **one** file: `script.deobf.lua`. The analysis, the recovered
strings and the recovery report stay in `work/<job>/` and come attached only
when you ask with `.full`.

Accepted uploads: `.lua`, `.luau`, `.txt`, `.lua5`, `.zip`. Zip uploads are
unpacked and the biggest Lua member is used. Links must be plain
HTTP(S) resolving to a public address — the downloader refuses private, loopback
and link-local targets, follows at most 5 redirects, and stops at
`MAX_FILE_MB`.

## Engines

| engine | covers | runtime |
|---|---|---|
| `bytecode` | raw `.luac` / Luau bytecode → source (Rust luadec) | bundled exe |
| `luraph15` | Luraph v15 / v15.1, devirtualization + polish | node + bundled luau |
| `ib1` | IronBrew1 static payload → IR + all constants | python |
| `luraph14` | Luraph v14 / v15 / v15.1 / v15.2, generic VM traces, IronBrew 1 | python + bundled luau |
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

## Result quality

Every engine output is scored before it is allowed to end the job
(`core/quality.py`). The pipeline no longer ships the first thing that comes
back: it holds the best candidate and keeps walking the chain while a better one
is still possible.

| signal | effect |
|---|---|
| code lines (comments excluded) | the main weight; more structure wins |
| parses with `luau-ast` | bonus — a lift that does not parse is still valid Lua 5.1 |
| `error("devirt: …")` / unlifted markers | penalty, and the lift can never be *complete* |
| engine declared `partial` | no completion bonus, and the chain keeps looking |
| literal/behaviour dump (`strings`, `unveilr`) | scored as a report, never as source |

The chain stops early when the result is structurally complete, or when the
engine that owns the detected fingerprint returns a hole-free lift — nothing
later has a better claim on that family. A partial result is still delivered
when no engine does better, but it is labelled `Partial`, the Discord embed
shows the score, and `job.json` records it.

## Recovery report (`trace.txt`)

Every job writes a `trace.txt` next to the artifact (`core/trace.py`), but the
Discord reply only attaches **the deobfuscated file** — one file, nothing else.
The side files come back on demand with `.full`:

| command | attachments |
|---|---|
| `.l`, `.get`, `.trace` | `deobf.lua` only |
| `.full` | `deobf.lua` + `strings.txt` + `analysis.txt` + `trace.txt` |
| any command, no lift at all | `trace.txt` only |

The same files always sit in `work/<job>/` on disk, whatever the command.

```
-- recovery report
input: +1 Speed Keyboard Escape.lua
sha256: 58e9c396dc40ddaf46c2c8f26248c079c30f1b05c56419a29c7729cf2eda673b
size: 543745 bytes

-- detection
family: Luraph v15 header
confidence: 0.99
evidence: Luraph v15 header (0.99); Luraph VM dispatch shape (0.80)

-- lift
engine: luraph15
score: 0.74
code lines: 5403
parses: True
opaque blocks: 1
complete: False

-- recovered from the input (static, nothing executed)
literals: 2000 (capped at 2000)
urls: 1
decoded payloads: 0

-- urls
  https://lura.ph/

-- indicators
  [HIGH] Executor HTTP API x6
  ...

-- why this is not a full lift
  1 unlifted block(s) kept as placeholders
```

When no engine returns a complete lift, the pipeline also runs the static
`strings` engine into a scratch directory (`work/<job>/recover/`) purely to fill
the *recovered* section, and keeps the result as `strings.txt`. Nothing is
executed for that — it is literal, URL and payload extraction from the input
bytes.

## Tests

`tests/corpus/` holds labelled samples per family and `tests/run_corpus.py`
runs the whole matrix. `tests/run_terrorlua.py` pulls real samples from
`terrorlua/obfuscator-samples` (3 per family, largest under the size cap) and
measures the engines, skipping the universal fallbacks:

```powershell
.\.venv\Scripts\python.exe tests\run_corpus.py --write
.\.venv\Scripts\python.exe tests\run_corpus.py -f wearedevs
.\.venv\Scripts\python.exe tests\run_terrorlua.py --per-family 3 --max-kb 200
.\.venv\Scripts\python.exe tests\test_quality.py
.\.venv\Scripts\python.exe tests\test_trace.py
.\.venv\Scripts\python.exe tests\test_three_maker.py
```

## Requirements

Windows is the supported host: the bundled engine binaries (`luadec.exe`,
`luau.exe`, `lune.exe`, `LuaAnalysis.Ironbrew2.exe`, `MoonsecDeobfuscator.exe`)
are Windows builds. On Linux the bot still starts, but every engine that needs
one of those binaries reports itself as missing.

| runtime | version | needed by | check |
|---|---|---|---|
| Python | 3.10+ (3.13 tested) | everything | `python --version` |
| Node.js | 18+ | `luraph15`, `prometheus`, `psu`, `luaobfuscator`, `moonveil`, `ironveil` | `node --version` |
| .NET | 8.0 runtime | `ib2net`, `f77`, `moonsec` | `dotnet --list-runtimes` |
| Java | 8+ (optional) | `moonsec` fallback decompiler only | `java -version` |

Python packages (`requirements.txt`): `discord.py`, `python-dotenv`, `aiohttp`
and `luaparser` — the last one is used by the `ironbrew2` engine, not by the bot
itself.

Everything else ships in the repository: the 25 engine trees in `engines/`, the
Windows Luau toolchain in `tools/`, `unluac.jar` for MoonSec, and the
`node_modules` the JS engines need. There is nothing to download after cloning.

Missing runtimes degrade instead of breaking: without Node the chain loses six
engines, without .NET it loses three. `--engines` names exactly what is missing
and the pipeline falls through to the next engine instead of failing the job.

## Install

```powershell
git clone https://github.com/Ryanabcraft/rex-deob
cd rex-deob
powershell -ExecutionPolicy Bypass -File install.ps1
```

`install.ps1` checks the four runtimes, builds `.venv`, installs
`requirements.txt`, prints the engine readiness table, and copies `.env.example`
to `.env` when there is none. It prefers the `py -3` launcher and locates the
venv interpreter in either `.venv\Scripts\` or `.venv\bin\`, so an msys/git-bash
`python` on PATH does not break it.

Then paste the token and start it:

```powershell
notepad .env             # DISCORD_TOKEN=...
.\start.bat              # or: .\.venv\Scripts\python.exe bot.py
```

## Discord setup

1. <https://discord.com/developers/applications> → **New Application** → **Bot**.
2. **Reset Token**, copy it into `.env` as `DISCORD_TOKEN=`.
3. Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**.
   The bot reads message text to find attachments, links and pasted blocks; with
   this off it receives empty messages and every command looks ignored.
4. **OAuth2 → URL Generator**: scope `bot`, permissions `Send Messages`,
   `Attach Files`, `Embed Links`, `Read Message History`. Open the generated URL
   and invite the bot.
5. Start it, then type `.engines` in a channel it can read. Every line should say
   `ok`; anything else prints the missing file or runtime.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `DISCORD_TOKEN missing` | no `.env`, or the value is empty | copy `.env.example`, paste the token |
| commands do nothing, no error | Message Content intent is off | enable it in the Developer Portal and restart |
| `engine unavailable: node not found` | Node not on PATH | install Node 18+, reopen the shell |
| `missing bin/luau.exe`, `missing lua5.1` | engine tree incomplete | re-clone; do not delete files inside `engines/` |
| `detected X; no engine for it yet` | the family has no engine | expected for Boronide, LPS, MoonVeil 1.x, PSU 5.x — the reply still comes from `unveilr`, then `strings` |
| `failed: tried …` | every engine in the chain refused the input | run `.detect` first, then set `KEEP_JOBS=1` and read the per-job log |
| a job takes minutes | Luraph v15 traces and devirtualizes | normal; 500 KB scripts take a few minutes by design |
| `output does not parse with luau-ast` | the engine emitted Lua 5.1 the Luau parser rejects | informational only, the file is still usable |
| `venv python not found` | unusual Python install layout | delete `.venv` and rerun `install.ps1` |

Set `KEEP_JOBS=1` in `.env` to keep `work/<job>/` — the input, every engine log,
`analysis.txt` and `job.json` — after the reply is posted. That is the first
place to look when an engine refuses a script.

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
core/               detection, adapters, pipeline, quality scoring, trace report
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
- The recovery header in `trace.txt` separates script blocks from the
  obfuscator's own runtime blocks, so the percentage is the script's, not the
  VM's. A small hub typically reads 100%; a stub whose payload is decrypted
  only inside a live game session stops at the instruction that builds it, and
  no amount of budget fixes that — pass `--game-id` (see `engines/luraph15/SETUP.md`).
- Compare block counts, not percentages, between two scripts: 100% of a
  three-function stub is three functions, while 69% of a 36-function hub is
  13 000 lines of real code with the holes marked in place.
- MoonSec returns both `deobf.lua` and `deobf.luac` when the decompiler runs.
- Centurion may report *partial* when a script needs live anti-tamper answers;
  the lift is still sent alongside the trace.
- Use it on code you own or are allowed to review. It exists to show what a
  protected script does before someone runs it.

Engine sources come from their authors and keep their original licenses inside
each `engines/*` directory.

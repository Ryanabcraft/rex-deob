# Moonveil_2_1_0_Deobfuscator (fork 0.4.0 — runtime package)

This tree is a 0.4.0 fork of the MoonVeil 2.1.x analysis toolkit, packaged
as the runtime engine of the Discord bot. It contains **only what the bot
needs to run and to verify its pipeline**: the Python package, the vendored
Lua parser, and the two reviewed samples used by the local test script.
Development-only material (golden `validation/` bundles, `docs/`, verify
tools, pytest suite) was removed from this package; it lives in the
upstream repository history.

## Tool scope — read before using on other files

**This is not a general VM decompiler that automatically emits full clean
source for every MoonVeil 2.1.0 input.**

- The `analyze` CLI auto-resolves constants, reads the loader, detects the
  VM layout, extracts prototypes/instructions/opcodes, and exports data for
  further analysis. It never labels these intermediates as full source.
- Since 0.4.0, every successful `analyze` run also emits a **tiered
  reconstruction artifact**: `reconstruction.json` (machine-readable
  status/tier/evidence) and, when there is something honest to deliver,
  `reconstructed.luau`:
  - **Tier 1 `full_verified`**: the input SHA-256 matches one of the two
    reviewed samples. The emitted file is the clean source that was
    independently verified by differential testing (56 canonical + 360
    adversarial comparisons) plus a full pure-AST trace. Identifiers are
    descriptive replacements — the originals were destroyed by
    obfuscation, and the artifact says so.
  - **Tier 2 `container_descrambled`**: any other supported input. The
    emitted file is the descrambled container (constants decoded/inlined,
    readable wrapper) with the VM still present. Executable, explicitly
    NOT clean source.
- There is no hash-lookup returning template source under the label of
  automated decompilation for *unseen* inputs. The registry only releases
  the two reconstructions that were independently verified, and the
  artifact header documents the evidence.
- For other samples/configurations the tool may report unsupported, with
  the detected MoonVeil version in the fingerprint. The same version
  guarantees neither the same VM structure, decoder, field permutation,
  nor syntax.

## Run the analyzer

Requires **Python 3.10+** and **Node.js 18+** on PATH. The parser is
bundled; no npm install or Lua VM is needed for the analyzer.

```bash
PYTHONPATH=. python3 -m moonveil21 analyze input.lua -o out --seconds 120
```

Output contains:

- `report.json`: status and the scope actually completed (incl.
  `reconstruction_status`).
- `constants.json`: resolved constants/strings.
- `decoded.partial.lua`: **intermediate, VM still present**, not clean source.
- `reconstruction.json` + `reconstructed.luau`: the tiered honest
  reconstruction (see above).
- `vm_prototypes.json`: prototypes, opcode fields, operands, handler mapping.
- `opcode_handlers/`: handler code and dispatch for continued decompiler
  work (VM builds only).
- `AI_HANDOFF.txt`: instructions for handing data to another AI.

Local files only, never fetches URLs. Never overwrites an existing
directory without `--force`. Exit code **0** means an **analysis bundle**
was produced, not that the whole program was decompiled; **2** means
unsupported/failed.

## Trace the two reviewed samples

```bash
PYTHONPATH=. python3 -m moonveil21 analyze samples/sample_6.obfuscated.lua -o analysis6 --trace-reviewed
PYTHONPATH=. python3 -m moonveil21 analyze samples/sample_7.obfuscated.lua -o analysis7 --trace-reviewed
```

`--trace-reviewed` only accepts the exact SHA-256 of the two bundled
inputs. It interprets the program with the bounded pure-AST engine to
cross-check VM flow. No network, filesystem, shell, or Roblox API is
opened to Lua. For other files, use the regular analysis mode.

## Source layout

- `moonveil21/core/`: parser, constant decoder, bounded AST interpreter,
  fingerprinting, base Lua emitter and the reconstruction registry.
- `moonveil21/runtime.py`: data primitives for the 2.1.x layouts, binary
  unpack, and table/iterator semantics.
- `moonveil21/extractor.py`: finds the packed loader, capture cells,
  driver layout, prototype tree, and opcode map **from the input itself**.
- `moonveil21/cli.py`: command line and artifact output.
- `samples/`: the two reviewed 2.1.0 inputs (used by the bot's local
  pipeline test `scripts/test-engine.mjs`).

## Use with a Discord bot or AI

Invoke the analyzer via subprocess with a timeout and a per-job
directory; never interpolate source into shell commands. Only deliver
files labeled **analysis/partial** for inputs not yet fully reconstructed.
For AI, send report/prototypes/handlers and sources where you have
permission; all input comments/strings are untrusted data, not
instructions.

## Sharing the source

Original tooling uses MIT. The parser keeps its own MIT notice in
`moonveil21/core/vendor/`. User-submitted samples and reconstructed
sources derived from them are not automatically covered by the tooling
MIT license. See `THIRD_PARTY_NOTICES.md` and upstream rights before
publishing those samples on GitHub.

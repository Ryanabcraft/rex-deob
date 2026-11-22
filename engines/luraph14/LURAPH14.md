# Luraph v14 (`deobf/obfuscators/luraph_v14/`)

> Plugin adapted from the v15 one for **Luraph Obfuscator v14.x** (tested on
> v14.2: `BloxFruitFreeV3.lua`, 2.17 MB, acsu123/HohoV2). Reference, not a
> changelog; hard limit ~500 lines.

## What works

- **detect()**: the `-- This file was protected using Luraph Obfuscator v14.x`
  header (1.0); without a header, `return(function(<many 1-letter params>)`
  + Luau binary literals (`0b...`) + `continue` (0.8).
- **Static VM map** (`vmmap.py`, adapted):
  - Dispatch loop shape: `while true do local INSTR=(ARR[PC]); local OP=(INSTR[6]);
    PC=(PC+1); if-tree end` — the if-tree sits at body index 3 (v15: 1), tests
    the **opcode variable** (body[1]'s target), conditions are wrapped in
    `not(...)` and paren groups. This build's luau-ast JSON stores groups as
    `AstExprGroup{expr=...}` (not `expression`).
  - Closure maker: `function(proto, upvals)` — **the proto is parameter 0**
    (v15: parameter 1 after the VM object). The proto is the parameter read
    with constant keys in the maker body (`h[1]`, `h[3]`, ...); constant-key
    reads INSIDE nested VM closures are instruction-field reads of shadowing
    locals and must not count (`_maker_params` skips nested functions).
  - VM closure: `function(...)` (vararg, no args) assigned by
    `_ = (function(...) ... end)` — **paren-wrapped**, so the binding
    statement's value is an `AstExprGroup` (ungroup before the identity check).
    The VM closure may sit below the maker through plain `function()` wrappers;
    `maker_info`/`closure_makers` find the binding assign at the VM closure's
    own function depth, not at maker_depth+1.
  - v14.2 file: **one maker** (`function(h,e)`, 2.09 MB region), **10 VM
    implementations** (1 payload VM with 131 handlers + 9 small fast-call /
    upvalue-close helpers with 2-4 handlers), all sharing it. Protos:
    `self = {[1]=numparams, [2]=L (wide operand numbers), [3]=d (instruction
    tables), [4]=pc start, [5]=constants}`. Instruction tables: fields 1-7,
    `[6]` = opcode; fields are **lazily decoded** (per-instruction `mt` with
    `__index` = a decoder VM closure, `mtf: {lf, pf}`).
- **Capture pipeline** (`driver.py`): `patch_v14` = entry hooks + maker
  captures + `__SNAP` statements, all spliced from ONE pristine-AST position
  list. The maker capture blocks must not be re-analyzed: inserted after the
  closure assign they would become the last statement at the maker level and
  hide the assign from `maker_info` (v15 avoids this by always analyzing the
  pristine file; the v14 snapshot ids are sequence-derived and identical on
  both sides).
- **Main-level upvalues**: v14 has no VM object; the makers reference the
  bootstrap's main-level locals (`u`, `c`, `j`, `D`, the bit-op tables, ...).
  `vmmap.maker_upvalues` computes the transitive set (a captured helper's own
  upvalues are needed to run its source). Their **final** values are captured
  by `patch_snapshots`/`main_snapshots`: after every main top-level statement
  the driver inserts `if __SNAP then __SNAP[id]=name; ... end` (table ops
  only); ids are static (`vm.snap_ids`), shadowing handled by visibility
  order. envlog pre-creates the global (`E.__SNAP = CFG.devirt and {} or
  false`) — a script-side `__SNAP={}` would read an undefined-global stub and
  never create the table. The run may abort mid-payload: the lifter walks the
  per-local id list backwards and uses the last snapshot that actually ran.
  The capture attach `__PA[pv].__SN=__SNAP` puts the table in every proto cap.
- **Behaviour trace**: works end to end (12 MB / 524k statements on the v14.2
  sample: services, UI construction, the key-system HTTP flow, config tables).

## What does not work yet (devirtualization)

`Program`/`Stepper`/`ProtoLifter` run and lift (the bootstrap helpers come
out as readable Luau), but the **payload walk derails**:

- The v14 CALL/multret handlers (`op 30/84/88`-ish) adjust the return top with
  `L,X = c(...)  -- c = select('#',...), {...}` and then run numeric loops
  over `L` (`for h=e,L do d+=1 end`). With a symbolic callee the bound stays
  symbolic and luasym raises "numeric for with symbolic bounds" — the v15
  stepper models these as IR `SetList`/packs instead of executing the loop.
  Porting that modeling is the remaining work item.
- `root_callee` is unreliable here: the attribution chain of the script's own
  statements has no pid-1 segment (the chain stops at the j-wrapper), so the
  recorded root callee is a banner-loadstring trampoline. The real script main
  is the **outermost proto of the first attributed statement whose chain has
  no pid-1 segment** (sample: pid 14). envlog's `rootCallee` logic would need
  a `mainCallee` variant.
- Decoder closures (`mtf`) lift as child protos on the same big VM; their
  operand lazy-decoding through requests works in principle (paths exist) but
  the walk dies before issuing them.

## Sample facts (BloxFruitFreeV3.lua)

- 2.17 MB, 1 line + mangled header (restore the newline after the header
  comment, `restore_header_newline`).
- Bootstrap: state machine over `I/m/Q` tables with `f.Q/f.u/f.r/f.J` bit
  ops from main's parameters; `local J = <2.08 MB call>` builds and runs
  everything.
- The file's tail contains **plain-text script fragments** (the aimbot
  `__namecall` hook: `ABLE_TO_AIM_BOT`, `InstaAimEnabled`, `ToAimPos`,
  `getrawmetatable(game)` / `newcclosure` — with `\110\118`-escaped strings).
  Luraph v14.2 leaves some functions un-virtualized; those are original
  source recoverable directly.
- Environment: the script checks `identifyexecutor()`, requests
  `http://httpbin.org/get` and `https://hohocomunity.xyz/api/info` (the HohoV2
  key API), then waits (UI loop) — the run ends in that wait without a key.

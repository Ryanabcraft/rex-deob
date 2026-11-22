#!/usr/bin/env python3
"""IronBrew3 static dump: serialized VM payload -> constants + opcode IR.

Opcode semantics are polymorphic per build, so the dump is honest: every
prototype with its instruction stream, plus every constant (strings that were
decrypted by the loader are included here when the profile decodes).
"""
import sys

from ib3parse import IB3Error, parse_ib3, find_root


def main(src_path: str, out_path: str) -> int:
    with open(src_path, "r", encoding="latin-1", errors="replace") as f:
        src = f.read()
    try:
        p = parse_ib3(src)
    except IB3Error as exc:
        print("IB3: " + str(exc), file=sys.stderr)
        return 1
    root = find_root(p.prototypes)
    lines = [
        "-- IronBrew3 static dump (opcode semantics are per-build)",
        f"-- profile: {p.profile}",
        f"-- prototypes: {len(p.prototypes)}",
        f"-- constants: {len(p.constants)}",
        f"-- root candidate: fn_{root}",
        "",
        "-- constants",
    ]
    for i, k in enumerate(p.constants):
        if isinstance(k, str):
            lines.append(f"K{i} = {k!r}")
        else:
            lines.append(f"K{i} = {k!r}")
    lines.append("")
    for i, fn in enumerate(p.prototypes):
        lines.append(f"-- prototype {i} params={fn.params} ins={len(fn.instructions)}")
        lines.append(f"local function fn_{i}(...)")
        for pc, x in enumerate(fn.instructions, 1):
            lines.append(f"    -- [{pc}] op={x.opcode} jump={x.jump} "
                         f"a={x.a} b={x.b} c={x.c} extra={x.extra!r}")
        lines.append("end")
        lines.append("")
    lines.append(f"return fn_{root}(...)")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"IB3: {len(p.prototypes)} prototypes, {len(p.constants)} constants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))

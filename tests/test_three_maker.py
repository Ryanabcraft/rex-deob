#!/usr/bin/env python3
"""Self-check for three-level VM makers (Luraph v15.1/v15.2).

The instrumentation records the middle function's parameters as __m1..__mN per
closure creation; the lifter rebuilds a closure the run never made from those
numbers, or from the constants at its creation site.

    python tests/test_three_maker.py
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(os.path.dirname(HERE), "engines", "luraph15")
sys.path.insert(0, os.path.join(ENGINE, "core"))
sys.path.insert(0, ENGINE)

from obfuscators.luraph_v15 import devirt, driver, vmmap  # noqa: E402

STUB = os.path.join(HERE, "corpus", "luraph151", "luraph151_stub.lua")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    src = open(STUB, encoding="latin-1").read()
    patched = driver.patch_entries(src, STUB)
    check("__mn=" in patched, "no __mn recorded for the middle maker")
    check(patched.count(".__m") >= 2, "no __mN argument recorded")

    vm = SimpleNamespace(
        maker={"args": [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]},
        info={"mode": "dispatch_local", "chain": [None]},
    )
    up = "UPVALS"
    got = devirt.VMModel.maker_args(vm, None, None, up, cap={"__m1": 7},
                                    site=[None, 9, None, None])
    check(got[:2] == [7, 9], f"capture/site fallback wrong: {got[:2]}")
    check(got[2:] == [up, up], f"unrecorded slots must stay upvals: {got[2:]}")

    plain = devirt.VMModel.maker_args(vm, None, None, up, cap={}, site=None)
    check(plain == [up] * 4, f"no numbers and no site must stay upvals: {plain}")

    check(hasattr(vmmap, "maker_info"), "vmmap lost maker_info")
    print("three_maker: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

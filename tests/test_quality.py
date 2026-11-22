#!/usr/bin/env python3
"""Self-check for the output quality scorer.

    python tests/test_quality.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import quality  # noqa: E402

BODY = "\n".join(f"local v{i} = {i} + 1" for i in range(120))
GOOD = "\n".join(["local function main()"]
                 + [f"    local x{i} = {i}" for i in range(60)]
                 + ["    return x0", "end", "main()"])


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    good = quality.assess(GOOD, parses=True)
    check(good.acceptable, "complete lift must be acceptable")
    check(good.complete, "parses + no holes + 60 lines must be complete")
    check(not good.report, "control flow present, not a report")
    check(good.score > 0.8, f"complete lift scores high, got {good.score}")

    holey = quality.assess(
        GOOD.replace("    return x0", '    error("devirt: unlifted block")'),
        parses=True, partial=True)
    check(holey.placeholders >= 1, "placeholder detected")
    check(not holey.complete, "a placeholder means not complete")
    check(holey.score < good.score, "holes must score below a clean lift")

    broken = quality.assess(GOOD, parses=False)
    check(broken.score < good.score, "unparseable scores below parseable")
    check(not broken.report, "unparseable is still source, not a report")

    dump = quality.assess(
        "\n".join(f"local _s{i} = 'literal {i}'" for i in range(400)),
        parses=True, partial=True)
    check(dump.report, "literal dump is a report")
    check(not dump.complete, "report is never the source")
    check(dump.score < good.score, "report scores below source")

    empty = quality.assess("-- nothing here\n", parses=True, partial=True)
    check(not empty.acceptable, "no code lines = not acceptable")

    tiny = quality.assess("return 1\n", parses=True)
    check(tiny.acceptable and tiny.complete, "small script can be complete")

    print(f"quality: ok  good={good.score} holey={holey.score} "
          f"broken={broken.score} dump={dump.score}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

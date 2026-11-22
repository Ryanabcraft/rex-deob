#!/usr/bin/env python3
"""Self-check for the recovery report writer.

    python tests/test_trace.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from core import trace  # noqa: E402

STRINGS = "\n".join([
    "local _u0 = 'https://example.com/a.lua'",
    "local _s0 = 'hello'",
    "local _s1 = 'world'",
    "local _d0 = 'print(1)'",
])


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trace-")
    src = os.path.join(tmp, "in.lua")
    with open(src, "w", encoding="utf-8") as f:
        f.write('local a = "https://example.com/a.lua"\n')
    with open(os.path.join(tmp, "strings.txt"), "w", encoding="utf-8") as f:
        f.write(STRINGS)

    result = SimpleNamespace(
        workdir=tmp, quality=None, engine=None, partial=True, seconds=1.5,
        detection=SimpleNamespace(family="unknown", confidence=0.0,
                                  evidence=[]),
        analysis=None, note="no engine produced output", main=None,
    )
    text = trace.build(src, result, "")
    check("sha256:" in text, "digest missing")
    check("literals: 2" in text, f"literal count wrong:\n{text}")
    check("urls: 1" in text, "url count wrong")
    check("decoded payloads: 1" in text, "payload count wrong")
    check("https://example.com/a.lua" in text, "url not listed")
    check("no engine produced output" in text, "reason not carried")

    path = trace.write(tmp, text)
    check(os.path.exists(path), "trace.txt not written")

    capped = trace.build(src, result, "")
    check("capped" not in capped, "cap note must not fire below the cap")

    print("trace: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

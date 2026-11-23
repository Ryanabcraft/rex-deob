#!/usr/bin/env python3
"""Self-check for the pdeobf cleanup on a big file.

A 354 KB pdeobf output used to keep `limpar` busy for minutes: the alias pass
re-masked every line on every round, and the dead-local pass rebuilt the whole
file once per declaration. Both are quadratic, and both are what made the bot's
Discord heartbeat stall. This keeps them bounded.

    python tests/test_cleaner_speed.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "core"))

from core.cleaners import clean_pdeobf  # noqa: E402

# roughly the shape that blew up: thousands of lines, hundreds of aliases,
# string literals containing names that must not be rewritten
LINES = ['local a0 = game', 'local b0 = a0:GetService("Players")']
for i in range(1, 900):
    LINES.append('local a%d = b0' % i)
    LINES.append('local b%d = a%d.Name' % (i, i))
    LINES.append('print(b%d, "%s a%d")' % (i, "x" * 40, i))
LINES.append('local dead = "unused"')
LINES.append('return b0')


def main() -> int:
    texto = "\n".join(LINES) + "\n"
    t0 = time.time()
    saida = clean_pdeobf(texto)
    dt = time.time() - t0
    if dt > 20.0:
        raise AssertionError("cleanup took %.1fs on a %.0f KB file" % (dt, len(texto) / 1024.0))
    if not saida.endswith("\n"):
        raise AssertionError("output must end with a newline")
    if 'game:GetService("Players")' not in saida:
        raise AssertionError("alias inlining did not reach the service call")
    # a name inside a literal is not an identifier: it must survive untouched
    if '"%s a1"' % ("x" * 40) not in saida:
        raise AssertionError("rewrote text inside a string literal")
    print("cleaner_speed: ok  %d KB in %.2fs" % (len(texto) / 1024.0, dt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

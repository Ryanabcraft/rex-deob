"""Luraph v14 plugin: devirtualizer + behaviour trace (adapted from the
v15 plugin for Luraph Obfuscator v14.x, e.g. v14.2 BloxFruitFreeV3)."""
import argparse
import re
import sys

from obfuscators.base import Obfuscator

HEADER = re.compile(r"This file was protected using Luraph Obfuscator v(\d+)(?:\.(\d+))?")
HEADER_LINE = re.compile(r"\s*--[ \t]*This file was protected using Luraph Obfuscator v[\d.]+[ \t]*"
                         r"\[https?://lura\.ph/?\]")


class LuraphV14(Obfuscator):
    name = "luraph_v14"
    label = "Luraph v14"
    doc = "LURAPH14.md"

    def detect(self, source):
        m = HEADER.search(source[:500])
        if m:
            return 1.0 if m.group(1) == "14" else 0.3
        # header stripped: v14.x's shape is `return(function(<many 1-letter
        # params>)...` with a numeric state-machine bootstrap over a shared
        # constant table, plus Luau binary literals (0b...) and `continue`.
        head = source.lstrip()[:1200]
        if head.startswith("return(function(") and re.search(r"\b0[bB][01_]{2,}\b", head) \
                and re.search(r"\bcontinue\b", source[:200000]):
            return 0.8
        return 0.0

    def add_arguments(self, ap):
        g = ap.add_argument_group("Luraph v14")
        # the v15 plugin registers the same flags; whichever loads first wins
        def opt(*a, **kw):
            try:
                g.add_argument(*a, **kw)
            except argparse.ArgumentError:
                pass
        opt("--no-hooks", action="store_true",
            help="do not instrument VM functions (no anti-tamper trap attribution, no lifting)")
        opt("--max-runs", type=int, default=12, help="maximum number of trace runs (trap reruns)")
        opt("--devirt-rounds", type=int, default=200,
            help="max lift + constant-request rounds (default 200; stops early when no new "
                 "constants turn up)")

    def deobfuscate(self, job):
        from obfuscators.luraph_v14 import driver
        fixed = restore_header_newline(job.source)
        if fixed != job.source:
            print("[*] header comment ran into the code (lost newline): split it", file=sys.stderr)
            job.source = fixed
            job.source_path = job.write(job.path(".src.lua"), fixed, encoding="latin-1")
        return driver.run(job)


def restore_header_newline(source):
    """Pasted copies can lose the newline after the `-- This file was
    protected ... [https://lura.ph/]` line; put it back."""
    m = HEADER_LINE.match(source)
    if m and source[m.end():m.end() + 1] not in ("", "\n", "\r"):
        return source[:m.end()] + "\n" + source[m.end():].lstrip(" \t")
    return source

#!/usr/bin/env python3
"""Family-agnostic static recovery: constants, escaped strings, URLs, blobs.

Never executes anything. Works on any Lua/Luau source or payload, so an
unknown obfuscator still yields the strings the loader was hiding (webhooks,
payload URLs, service names) plus a readable listing of decoded literals.
"""
from __future__ import annotations

import base64
import re
import sys

QUOTED = re.compile(r'"((?:[^"\\\n]|\\.){2,4000})"|\'((?:[^\'\\\n]|\\.){2,4000})\'')
ESC = re.compile(r"\\(\d{1,3}|x[0-9A-Fa-f]{2}|.)")
URL = re.compile(r"https?://[^\s'\"<>\\]{6,300}")
B64 = re.compile(r"[\"']([A-Za-z0-9+/]{60,}={0,2})[\"']")
HEXRUN = re.compile(r"\b(?:string\.char\s*\(|\\x)((?:[0-9A-Fa-f]{2}\s*,\s*){8,})")
DECRUN = re.compile(r"string\.char\s*\(\s*(\d{1,3}(?:\s*,\s*\d{1,3}){7,})\s*\)")

NAMED = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
         "v": "\v", "\\": "\\", '"': '"', "'": "'"}


def unescape(body: str) -> str:
    def one(m: re.Match) -> str:
        tok = m.group(1)
        if tok.isdigit():
            return chr(int(tok) & 0xFF)
        if tok.startswith("x") and len(tok) == 3:
            try:
                return chr(int(tok[1:], 16))
            except ValueError:
                return ""
        return NAMED.get(tok, tok)
    return ESC.sub(one, body)


def printable_ratio(s: str) -> float:
    if not s:
        return 0.0
    return sum(1 for c in s if 32 <= ord(c) < 127 or c in "\n\t") / len(s)


def scan(text: str) -> dict:
    literals: list[str] = []
    for m in QUOTED.finditer(text):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        dec = unescape(raw)
        if len(dec) >= 3 and printable_ratio(dec) > 0.85:
            literals.append(dec)

    hexdec: list[str] = []
    for m in HEXRUN.finditer(text):
        try:
            by = bytes(int(x, 16) for x in
                       re.findall(r"[0-9A-Fa-f]{2}", m.group(1)))
            dec = by.decode("utf-8", "replace")
            if printable_ratio(dec) > 0.85:
                hexdec.append(dec)
        except ValueError:
            pass
    for m in DECRUN.finditer(text):
        try:
            by = bytes(int(x) & 0xFF for x in m.group(1).split(","))
            dec = by.decode("utf-8", "replace")
            if printable_ratio(dec) > 0.85:
                hexdec.append(dec)
        except ValueError:
            pass

    urls = sorted({u.rstrip(".,);") for u in URL.findall(text)})

    b64: list[str] = []
    for m in B64.finditer(text):
        try:
            dec = base64.b64decode(m.group(1) + "==").decode("utf-8", "replace")
            if printable_ratio(dec) > 0.9 and len(dec) >= 10:
                b64.append(dec)
        except Exception:
            pass

    uniq = list(dict.fromkeys(literals))
    return {"literals": uniq, "hex": dict.fromkeys(hexdec),
            "urls": urls, "b64": b64}


def main(src_path: str, out_path: str) -> int:
    raw = open(src_path, "rb").read()
    text = raw.decode("latin-1", "replace")
    found = scan(text)
    lines = [
        "-- static string recovery (no execution; family-agnostic)",
        f"-- literals: {len(found['literals'])}  urls: {len(found['urls'])}"
        f"  decoded blobs: {len(found['hex']) + len(found['b64'])}",
        "",
    ]
    if found["urls"]:
        lines.append("-- urls")
        lines += [f"local _u{i} = {u!r}" for i, u in enumerate(found["urls"])]
        lines.append("")
    decoded = list(found["hex"].keys()) + found["b64"]
    if decoded:
        lines.append("-- decoded payloads")
        for i, d in enumerate(decoded[:200]):
            lines.append(f"local _d{i} = {d!r}")
        lines.append("")
    if found["literals"]:
        lines.append("-- literals")
        for i, s in enumerate(found["literals"][:2000]):
            lines.append(f"local _s{i} = {s!r}")
    if len(lines) <= 4:
        return 1
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"strings: {len(found['literals'])} literals, {len(found['urls'])} urls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))

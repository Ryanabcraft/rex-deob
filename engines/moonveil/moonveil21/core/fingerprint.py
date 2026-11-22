"""Cheap, regex-level fingerprinting of MoonVeil-family obfuscation output.

This never parses or executes anything; it only classifies raw bytes so that
(a) the wrapper gate can name the detected version in its rejection message
and (b) reports describe what the input looked like.

Empirically verified header forms (see docs/KNOWLEDGE_BASE.md):
  -- This script was generated using MoonVeil 2.1.0 ttps://moonveil.cc]
  -- This script was generated using MoonVeil 2.1.1 ttps://moonveil.cc]
  -- This script was generated using MoonVeil 2.0.26 ttps://moonveil.cc]
  -- This script was protected using the MoonVeil Obfuscator v1.4.5 ...
The real-world 2.x/1.4.x samples seen so far carry a mangled URL fragment
("ttps://moonveil.cc]"), while the vendor docs show "[https://moonveil.cc]";
both are accepted by the detector.
"""

from __future__ import annotations

import re

_HEADER = re.compile(
    rb"--\s*This\s+script\s+was\s+(?:generated|protected)\s+using\s+"
    rb"(?:the\s+)?MoonVeil(?:\s+Obfuscator)?\s*v?(\d+\.\d+\.\d+[\w.-]*)"
)
_VERSION_WORD = re.compile(rb"MoonVeil(?:\s+Obfuscator)?\s*v?(\d+\.\d+\.\d+[\w.-]*)")
_MEMO = re.compile(rb"\w+\.\w+\[\s*-?0x[0-9A-Fa-f]+\s*\]\s*or\s*\w+\s*:")
_BUNDLE = re.compile(rb"\breturn\s*\(\s*\{")
_PREAMBLE = re.compile(
    rb"local\s+\w+(?:\s*,\s*\w+)*\s*=\s*(?:getmetatable|type|pairs)\b"
)
_ESCAPED = re.compile(rb"\\[0-9]{1,3}")
_WHILE_TRUE = re.compile(rb"\bwhile\s+true\s+do\b")


def header_version(raw: bytes):
    """MoonVeil version string from the header comment, or None."""
    m = _VERSION_WORD.search(raw[:512])
    if not m:
        return None
    try:
        return m.group(1).decode("ascii")
    except UnicodeDecodeError:
        return None


def fingerprint(raw: bytes):
    """Structural hints about a possibly-MoonVeil input (never parses it)."""
    head = raw[:512]
    m = _HEADER.search(head)
    version = header_version(raw)
    hints = []

    if version:
        hints.append(
            "moonveil_header:"
            + ("2.x_bundle_wording" if m and b"generated" in m.group(0) else "1x_protected_wording"
               if m else "version_only")
        )
    if _BUNDLE.search(raw[:4096]):
        hints.append("return_table_bundle")
    if _PREAMBLE.search(raw[:4096]) and b"bit32" in raw[:4096]:
        hints.append("localized_builtin_preamble_1x")
    if _MEMO.search(raw):
        hints.append("memo_constant_cache")
    escaped = len(_ESCAPED.findall(raw))
    if escaped > 400:
        hints.append("byte_escaped_payload_%d" % escaped)
    if _WHILE_TRUE.search(raw):
        hints.append("state_machine_loops")

    return {
        "detected_obfuscator": "MoonVeil" if version else "unknown",
        "header_version": version,
        "header_wording": (
            "generated_using"
            if m and b"generated" in m.group(0)
            else "protected_using"
            if m
            else None
        ),
        "family_hints": hints,
        "memo_idiom": bool(_MEMO.search(raw)),
        "escaped_byte_sequences": escaped,
    }

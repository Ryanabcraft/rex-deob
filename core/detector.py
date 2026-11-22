"""Static fingerprinting of Roblox obfuscators.

Two layers, cheapest first:

1. Header comments — what each product stamps on its own output. Free and
   decisive when present.
2. Structural shapes — the VM preludes and guard strings that survive header
   stripping (the common case for leaked/cracked scripts). Without these a
   headerless IronBrew2 file is handed to the Luraph engine first and burns the
   whole budget before reaching the engine that actually handles it.

`Detection.labels` keeps every signature that matched, so an engine is still
eligible when it is not the top hit — the top hit only decides who runs first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SIGS: list[tuple[str | None, re.Pattern, float, str]] = [
    ("moonsec", re.compile(r"This file was protected with MoonSec|MoonSec\s*V?3"),
     0.99, "MoonSec V3 watermark"),
    ("luraph15", re.compile(r"This file was protected using Luraph Obfuscator v15"),
     0.99, "Luraph v15 header"),
    ("luraph15", re.compile(r"LPH_(?:JIT|ENCSTR|ENCFUNC|PRECHECK|NO_VIRTUALIZE)"),
     0.85, "LPH VM macros"),
    ("luraph14", re.compile(r"Luraph Obfuscator v1[0-4]\b"),
     0.95, "Luraph v14 header"),
    ("ib3", re.compile(r"--ironbrew3:tm:"),
     0.97, "IronBrew3 header"),
    ("ib1", re.compile(r"generated using ironbrew1"),
     0.97, "IronBrew1 header"),
    ("luraph14", re.compile(r"\bIronBrew\b|\bIB2\b"),
     0.85, "IronBrew signature"),
    ("prometheus", re.compile(r"wearedevs\.net/obfuscator"),
     0.97, "WeAreDevs header"),
    ("prometheus", re.compile(r"Prometheus Obfuscator|prometheus\.wtf"),
     0.70, "Prometheus watermark"),
    ("luarmor", re.compile(r"luarmor", re.I),
     0.90, "Luarmor mention"),
    ("luarmor", re.compile(r"_bsdata\d+\s*=\s*\{"),
     0.90, "Luarmor loader blob table"),
    ("luarmor", re.compile(r"api\.luarmor\.net|script_key\s*="),
     0.92, "Luarmor loader stub"),
    ("centurion", re.compile(r"Centurion v\d"),
     0.95, "Centurion tag"),
    ("hercules", re.compile(r"Obfuscated by Hercules v[\d.]+|WrapState\(BcToState\("),
     0.98, "Hercules v1.x"),
    ("ironveil", re.compile(r"Obfuscated using ironveil v[\d.]+"),
     0.98, "IronVeil"),
    ("moonveil", re.compile(r"MoonVeil Obfuscator v[\d.]+"),
     0.98, "MoonVeil"),
    ("moonveil", re.compile(r"generated using MoonVeil [\d.]+"),
     0.98, "MoonVeil 2.x"),
    ("luaobfuscator", re.compile(r"\bv7\s*\(\s*\"(?:\\.|[^\"\\])*\"\s*,\s*\"(?:\\.|[^\"\\])*\"\s*\)"),
     0.85, "LuaObfuscator Chaotic Good call"),
    ("luraph14", re.compile(r"does your environment support load/loadstring\?"),
     0.85, "Luraph v14 loader question"),
    ("synapsexen", re.compile(r"Synapse Xen v[\d.]+"),
     0.98, "Synapse Xen"),
    ("psu", re.compile(r"PSU Obfuscator\s*v?[\d.]+", re.I),
     0.98, "PSU"),
    ("wyn", re.compile(r"Protected by wYnFuscate"),
     0.98, "wYnFuscate"),
    ("f77", re.compile(r"77fuscator\s+[\d.]+"),
     0.97, "77fuscator"),
    (None, re.compile(r"herrtt'?s obfuscator"),
     0.96, "Boronide"),
    ("luaobfuscator", re.compile(r"LuaObfuscator\.com"),
     0.95, "LuaObfuscator"),
    ("orrxl4", re.compile(r"PROTECTED BY ORRXL4"),
     0.99, "Orrxl4 header"),
    (None, re.compile(r"[\"']\[\^!-uz\][\"']"),
     0.80, "LPS base85 charset"),
]

VM_SHAPE = re.compile(r"return\s+setmetatable\(\{")
VM_TABLE = re.compile(r"\[\d+\]\s*=\s*(?:bit32|buffer|string|table|math)\.\w+")
LONGTABLE = re.compile(r"\{\s*\"(?:\\\d{2,3}){4,}\"")
# wYnFuscate refuses to boot outside a loadstring-capable env; the guard is
# written as escaped bytes and survives header stripping.
_WYN_GUARD = re.compile(r"\\101\\110\\104\\097\\110\\099\\101\\100\\045\\118\\109")


def _ib2_shape(text: str) -> bool:
    """IronBrew2 prelude: localized string builtins + ldexp + concat.

    The exact prelude is re-ordered and renamed per build, so match the set.
    LuaObfuscator's Chaotic Evil output copies this prelude almost verbatim but
    ships string.rep; excluding it keeps the two apart.
    """
    head = text[:8192]
    if "string.rep" in head:
        return False
    return ("string.byte" in head and "string.char" in head
            and "string.sub" in head and "math.ldexp" in head
            and "table.concat" in head)


def _wyn_shape(text: str) -> bool:
    return bool(_WYN_GUARD.search(text[:65536]))


def _luraph_vm_shape(text: str) -> bool:
    return bool(VM_SHAPE.search(text[:262144])) and (
        bool(VM_TABLE.search(text[:8192])) or "LPH" in text[:8192])


def _prom_byte_pool(text: str) -> bool:
    head = text[:4096]
    return (text.count("\\1") >= 50 and "return(function(" in head[:200]
            and ("bit32" in text or "getgenv" in text))


SHAPES: list[tuple[str | None, object, float, str]] = [
    ("luraph15", _luraph_vm_shape, 0.80, "Luraph VM dispatch shape"),
    ("ib2net", _ib2_shape, 0.92, "IronBrew2 VM prelude"),
    ("wyn", _wyn_shape, 0.90, "wYnFuscate"),
    ("prometheus", _prom_byte_pool, 0.85, "Prometheus byte-pool dispatch"),
]


@dataclass
class Detection:
    engine: str | None
    confidence: float
    evidence: list[str] = field(default_factory=list)
    lua_like: bool = True
    family: str | None = None
    labels: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)

    def matches(self, families: tuple[str, ...] | list[str]) -> bool:
        """True when any signature pointed at one of these family labels."""
        return any(label in self.labels for label in families)


def looks_like_lua(text: str) -> bool:
    head = text[:4096].lstrip()
    if not head:
        return False
    if head.startswith("<!DOCTYPE") or head.startswith("<html"):
        return False
    printable = sum(1 for c in head if c.isprintable() or c in "\r\n\t")
    return printable / len(head) > 0.85


def is_prometheus_shape(text: str) -> bool:
    """Header stripped Prometheus/WeAreDevs: escaped byte pool + numeric dispatch."""
    return _prom_byte_pool(text)


def detect(text: str) -> Detection:
    """Rank static signatures over the leading 256 KB."""
    scan = text[:262144]
    hits: list[tuple[float, str | None, str]] = []
    for engine, pat, weight, label in SIGS:
        if pat.search(scan):
            hits.append((weight, engine, label))
    for engine, pred, weight, label in SHAPES:
        if pred(scan):
            hits.append((weight, engine, label))
    if LONGTABLE.search(scan):
        hits.append((0.55, "prometheus", "escaped constant-array table"))

    if not hits:
        return Detection(None, 0.0, ["no known signature"], looks_like_lua(text))

    hits.sort(key=lambda h: h[0], reverse=True)
    evidence = [f"{label} ({w:.2f})" for w, _, label in hits]
    ranked: list[str] = []
    for _, engine, _ in hits:
        if engine and engine not in ranked:
            ranked.append(engine)
    return Detection(hits[0][1], hits[0][0], evidence, looks_like_lua(text),
                     family=evidence[0].rsplit(" (", 1)[0],
                     labels=[label for _, _, label in hits], ranked=ranked)


def candidates_for(det: Detection, text: str) -> list[str]:
    """Ordered engines to try. Strongest signature first, then the full list."""
    from .engines import ORDER

    chain: list[str] = list(det.ranked)
    if det.engine == "luraph14" and "luraph15" not in chain:
        chain.append("luraph15")
    if det.engine == "luraph15" and "luraph14" not in chain:
        chain.append("luraph14")
    if det.engine == "luarmor" and "luraph15" not in chain:
        chain.append("luraph15")
    scan = text[:262144]
    if re.search(r"MoonSec", scan) and "moonsec" not in chain:
        chain.append("moonsec")
    for eng in ORDER:
        if eng.name not in chain:
            chain.append(eng.name)
    return chain

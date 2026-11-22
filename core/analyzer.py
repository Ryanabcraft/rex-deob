"""Indicator scan on deobfuscated source: what was the script hiding?

Static regex only. It answers the question the deobfuscation was for -- "what
does this thing actually do" -- not a guarantee.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RULES: list[tuple[str, str, re.Pattern, str]] = [
    ("HIGH", "Discord webhook exfil",
     re.compile(r"https://(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+"), ""),
    ("HIGH", "Executor HTTP API",
     re.compile(r"\b(?:HttpGet|HttpPost|http_request|syn\.request|fluxus\.request)\b"), ""),
    ("MED", "Remote payload URL",
     re.compile(r"https?://(?!discord)[\w.-]+/\S+"), ""),
    ("HIGH", "Process execution",
     re.compile(r"\bos\.execute\s*\(|\bio\.popen\s*\("), ""),
    ("HIGH", "Filesystem access",
     re.compile(r"\b(?:readfile|writefile|listfiles|appendfile|delfile|isfile)\s*\("), ""),
    ("HIGH", "Roblox auth cookie string",
     re.compile(r"ROBLOSECURITY|\.ROBLOSECURITY"), ""),
    ("HIGH", "Base64 blob (dropper)",
     re.compile(r"[\"'](?:[A-Za-z0-9+/]{300,}={0,2})[\"']"), ""),
    ("HIGH", "Luarmor script key",
     re.compile(r"script_key\s*=\s*[\"'][^\"']{8,}"), "mask"),
    ("MED", "Dynamic code loading",
     re.compile(r"\b(?:loadstring|load)\b"), ""),
    ("MED", "Environment manipulation",
     re.compile(r"\b(?:getfenv|setfenv|getgenv|setreadonly)\s*\("), ""),
    ("MED", "WebSocket stream",
     re.compile(r"wss?://|WebSocket"), ""),
    ("MED", "Key system gate",
     re.compile(r"\b(?:KeyAuth|key\s*system|validate\s*key|redeem)\b", re.I), ""),
    ("LOW", "Obfuscator marker left",
     re.compile(r"Luraph Obfuscator|wearedevs\.net|MoonSec|Prometheus"), ""),
    ("LOW", "Anti-tamper spin loop",
     re.compile(r"while\s+true\s+do\s+end"), ""),
]

WEBHOOK_MASK = re.compile(r"(webhooks/\d+/)([\w-]{4})[\w-]+")


@dataclass
class Finding:
    severity: str
    label: str
    count: int
    samples: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    findings: list[Finding]
    lines: int
    bytes: int

    @property
    def risk(self) -> str:
        for sev in ("HIGH", "MED", "LOW"):
            if any(f.severity == sev for f in self.findings):
                return sev
        return "CLEAN"

    def render(self) -> str:
        out = [f"risk: {self.risk}",
               f"lines: {self.lines}  bytes: {self.bytes}", ""]
        if not self.findings:
            out.append("no indicators matched")
            return "\n".join(out)
        for f in self.findings:
            out.append(f"[{f.severity}] {f.label}  x{f.count}")
            for s in f.samples:
                out.append("    " + s)
        return "\n".join(out)


def _mask(line: str) -> str:
    line = WEBHOOK_MASK.sub(r"\1\2…", line)
    return re.sub(r"(script_key\s*=\s*[\"'])[^\"']+", r"\1…", line)


def analyze(text: str) -> Analysis:
    lines = text.splitlines()
    findings: list[Finding] = []
    for sev, label, pat, mode in RULES:
        samples: list[str] = []
        count = 0
        for i, line in enumerate(lines):
            if len(line) > 4000:
                line = line[:4000]
            m = pat.search(line)
            if not m:
                continue
            count += 1
            if len(samples) < 3:
                snip = line.strip()[:160]
                samples.append(f"L{i + 1}: " + (_mask(snip) if mode != "raw" else snip))
            if count > 5000:
                break
        if count:
            findings.append(Finding(sev, label, count, samples))
    findings.sort(key=lambda f: {"HIGH": 0, "MED": 1, "LOW": 2}[f.severity])
    return Analysis(findings, len(lines), len(text))

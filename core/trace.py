"""Recovery report for jobs that did not reach a complete lift.

When no engine returns full source, the useful answer is not just "here is what
we could lift" — it is what the script *carries* and how far the lift got. This
module assembles that into one `trace.txt` next to the artifact:

    provenance   what was detected, which engine won, how the lift scored
    recovered    literals, URLs and decoded payloads pulled out of the input
    coverage     code lines, opaque blocks, whether the output parses

It never executes the input; the literals come from the `strings` engine, which
is static by design.
"""
from __future__ import annotations

import hashlib
import os
import re

URL = re.compile(r"https?://[^\s'\"<>\\]{6,300}")
LITERAL = re.compile(r"^local _([sud])\d+ = ", re.M)


def _counts(strings_text: str) -> tuple[int, int, int]:
    kinds = LITERAL.findall(strings_text)
    return (kinds.count("s"), kinds.count("u"), kinds.count("d"))


def build(input_path: str, result, body: str) -> str:
    """Render the report. `result` is the finished JobResult."""
    raw = open(input_path, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    q = result.quality
    det = result.detection

    strings_path = os.path.join(result.workdir, "strings.txt")
    strings_text = ""
    if os.path.exists(strings_path):
        with open(strings_path, encoding="utf-8", errors="replace") as f:
            strings_text = f.read()
    literals, urls_n, blobs = _counts(strings_text)
    urls = sorted({u.rstrip(".,);]}'\"") for u in URL.findall(strings_text)})

    out = [
        "-- recovery report",
        f"input: {os.path.basename(input_path)}",
        f"sha256: {digest}",
        f"size: {len(raw)} bytes",
        "",
        "-- detection",
        f"family: {det.family or 'unknown'}",
        f"confidence: {det.confidence:.2f}",
        f"evidence: {'; '.join(det.evidence[:4]) or 'none'}",
        "",
        "-- lift",
        f"engine: {result.engine or 'none'}",
        f"partial: {result.partial}",
        f"seconds: {result.seconds:.1f}",
    ]
    if q:
        out += [
            f"score: {q.score:.2f}",
            f"code lines: {q.code_lines}",
            f"parses: {q.parses}",
            f"opaque blocks: {q.placeholders}",
            f"complete: {q.complete}",
        ]
    out += [
        "",
        "-- recovered from the input (static, nothing executed)",
        f"literals: {literals}" + (" (capped at 2000)" if literals >= 2000 else ""),
        f"urls: {urls_n}",
        f"decoded payloads: {blobs}",
    ]
    if urls:
        out += ["", "-- urls"] + [f"  {u}" for u in urls[:40]]
    if result.analysis and result.analysis.findings:
        out += ["", "-- indicators"]
        out += [f"  [{f.severity}] {f.label} x{f.count}"
                for f in result.analysis.findings[:12]]
    if not q or not q.complete:
        out += [
            "",
            "-- why this is not a full lift",
            f"  {result.note or q.reason if q else 'no engine produced output'}",
        ]
    if body:
        out += ["", f"-- artifact: {os.path.basename(result.main or 'deobf.lua')}"]
    return "\n".join(out) + "\n"


def write(job_dir: str, text: str) -> str:
    path = os.path.join(job_dir, "trace.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path

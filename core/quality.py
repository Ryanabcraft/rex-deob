"""Output quality scoring.

The pipeline used to take the first engine that produced anything, which meant a
partial lift could win over a complete one further down the chain. This module
turns an output into a comparable score so the pipeline can keep looking and ship
the best result instead of the first.

Everything here is static and cheap: line counts, placeholder markers, and the
Luau parse result the pipeline already computes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Engines that leave a hole where a virtualized block was.
PLACEHOLDER = re.compile(
    r"error\s*\(\s*[\"'](?:devirt|unlifted|unsupported|not implemented)|"
    r"--\s*(?:unlifted|unsupported|devirt)|"
    r"\bUNLIFTED\b",
    re.I,
)
# A decompiler's own scaffolding names: fine in a dump, a sign the lift is raw.
RAW_NAME = re.compile(r"\blocal\s+v\d+\s*=")
COMMENT = re.compile(r"^\s*--")
CONTROL = re.compile(r"\b(?:function|if|for|while|repeat|return|and|or|not)\b")

# Below this many code lines, a large input means the engine gave up early.
MIN_CODE_LINES = 20


@dataclass
class Quality:
    lines: int
    code_lines: int
    bytes: int
    parses: bool
    placeholders: int
    raw_names: int
    comment_lines: int
    report: bool
    score: float
    acceptable: bool
    complete: bool
    reason: str = ""


def count_code_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines()
               if ln.strip() and not COMMENT.match(ln))


def assess(text: str, parses: bool, partial: bool = False) -> Quality:
    """Score one engine output. `parses` comes from luau-ast in the pipeline."""
    lines = text.splitlines()
    code_lines = sum(1 for ln in lines if ln.strip() and not COMMENT.match(ln))
    comment_lines = sum(1 for ln in lines if COMMENT.match(ln))
    placeholders = len(PLACEHOLDER.findall(text))
    raw_names = len(RAW_NAME.findall(text))

    # A literals/behaviour dump: assignments with no control flow at all. Still
    # useful, but never "the source".
    report = ((code_lines > 10 and not CONTROL.search(text))
              or (code_lines > 5 and comment_lines > code_lines * 4))

    acceptable = code_lines > 0
    # Structural completeness: real code, no holes, and the engine did not
    # declare the lift approximate. Parseability is scored separately — an
    # engine can emit valid Lua 5.1 that the Luau parser rejects and still be
    # the right answer.
    complete = (acceptable and placeholders == 0 and not report and not partial)

    score = (
        0.40 * min(1.0, code_lines / 100.0)
        + 0.25 * (1.0 if parses else 0.0)
        + 0.15 * (0.0 if partial else 1.0)
        + 0.20 * (1.0 if complete else 0.0)
        - 0.40 * (1.0 if report else 0.0)
    )
    score -= min(0.25, placeholders * 0.01)
    score -= min(0.05, raw_names * 0.0005)
    score = round(max(0.0, min(1.0, score)), 4)

    reason = ""
    if not acceptable:
        reason = "no code lines"
    elif placeholders:
        reason = f"{placeholders} unlifted block(s) kept as placeholders"

    return Quality(
        lines=len(lines),
        code_lines=code_lines,
        bytes=len(text.encode("utf-8", "replace")),
        parses=parses,
        placeholders=placeholders,
        raw_names=raw_names,
        comment_lines=comment_lines,
        report=report,
        score=score,
        acceptable=acceptable,
        complete=complete,
        reason=reason,
    )

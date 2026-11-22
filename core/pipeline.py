"""Job orchestration: detect, run the engine chain, validate, collect artifacts."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import analyzer, config, detector
from .engines import ENGINES, ORDER, RunOutcome, _run

Progress = Callable[[int, str], Awaitable[None]]


@dataclass
class Attempt:
    engine: str
    ok: bool
    note: str = ""
    seconds: float = 0.0


@dataclass
class JobResult:
    ok: bool
    detection: detector.Detection
    engine: str | None = None
    trace: bool = False
    main: str | None = None
    artifacts: list[str] = field(default_factory=list)
    analysis: analyzer.Analysis | None = None
    note: str = ""
    partial: bool = False
    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0
    input_path: str = ""
    workdir: str = ""
    stdout: str = ""


def decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def is_bytecode(data: bytes) -> bool:
    if data[:1] == b"\x1b":
        return True
    if len(data) > 8 and data[0] <= 0x0F:
        head = data[:128]
        return head.count(b"\x00") >= 8
    return False


def new_workdir() -> str:
    os.makedirs(config.WORK, exist_ok=True)
    _prune(config.WORK, max_age_h=48)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(config.WORK, f"{stamp}-{os.getpid()}-{int(time.time() * 1000) % 1000}")
    os.makedirs(path, exist_ok=False)
    return path


def _prune(root: str, max_age_h: int) -> None:
    cutoff = time.time() - max_age_h * 3600
    for name in os.listdir(root):
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


def _code_lines(text: str) -> int:
    return sum(1 for ln in text.splitlines()
               if ln.strip() and not ln.strip().startswith("--"))


async def _parses(path: str) -> bool:
    ast = config.tool("luau-ast")
    if not os.path.exists(ast):
        return True
    log = os.path.join(os.path.dirname(path), "parse.log")
    code, to = await _run([ast, path], os.path.dirname(path), log, 90, quiet=True)
    try:
        os.remove(log)
    except OSError:
        pass
    return code == 0 and not to


async def _syntax_note(path: str) -> str:
    return "" if await _parses(path) else "output does not parse with luau-ast"


def _gather_extra(job_dir: str, main: str) -> list[str]:
    extras: list[str] = []
    for name in ("strings.txt", "report.json", "analysis.txt"):
        p = os.path.join(job_dir, name)
        if os.path.exists(p):
            extras.append(p)
    return extras


def _relocate(job_dir: str, outcome: RunOutcome) -> None:
    """Normalize engine side files (strings report, json report) into job_dir."""
    if not outcome.main:
        return
    import glob
    found = []
    for pat, dst in ((os.path.join(job_dir, "*.strings.txt"), "strings.txt"),
                     (os.path.join(job_dir, "*.report.json"), "report.json")):
        hits = [h for h in glob.glob(pat) if os.path.basename(h) != dst]
        if hits:
            found.append((max(hits, key=os.path.getsize), os.path.join(job_dir, dst)))
    for src, dst in found:
        try:
            shutil.move(src, dst)
        except OSError:
            shutil.copyfile(src, dst)


async def run_job(input_path: str, trace: bool = False,
                  force_engine: str | None = None,
                  progress: Progress | None = None,
                  max_engine_seconds: int | None = None,
                  skip_engines: set[str] | None = None) -> JobResult:
    started = time.time()
    job_dir = new_workdir()
    input_path = os.path.abspath(input_path)
    raw = open(input_path, "rb").read()
    bytecode = is_bytecode(raw)
    if bytecode:
        text = ""
        det = detector.Detection("bytecode", 1.0, ["bytecode magic"], True,
                                 family="Lua/Luau bytecode",
                                 labels=["Lua/Luau bytecode"], ranked=["bytecode"])
    else:
        text = decode(raw)
        det = detector.detect(text)

    async def say(pct: int, msg: str) -> None:
        if progress:
            try:
                await progress(pct, msg)
            except Exception:
                pass

    if not det.lua_like:
        return JobResult(False, det, note="input does not look like Lua source",
                         input_path=input_path, workdir=job_dir,
                         seconds=time.time() - started)

    await say(4, f"fingerprint: {det.family or det.engine or 'unknown'}")

    if force_engine:
        chain = [force_engine]
    elif bytecode:
        chain = ["bytecode"]
    else:
        chain = detector.candidates_for(det, text)
    result = JobResult(False, det, trace=trace, input_path=input_path, workdir=job_dir)
    attempts: list[Attempt] = []
    result.attempts = attempts

    for name in chain:
        eng = ENGINES.get(name)
        if eng is None:
            continue
        if skip_engines and name in skip_engines:
            attempts.append(Attempt(name, False, "skipped for this run"))
            continue
        if trace and name == "prometheus":
            attempts.append(Attempt(name, False, "no trace mode"))
            continue
        if trace and not eng.can_trace:
            attempts.append(Attempt(name, False, "no trace mode"))
            continue
        ok, why = eng.available()
        if not ok:
            attempts.append(Attempt(name, False, "unavailable: " + why))
            continue
        if not force_engine and eng.families and not det.matches(eng.families):
            attempts.append(Attempt(name, False, "no matching fingerprint"))
            continue
        budget = max_engine_seconds or config.TIMEOUTS.get(eng.timeout_key, 600)
        pct = 8 + int(84 * chain.index(name) / max(1, len(chain)))
        await say(pct, f"{eng.label} · step {chain.index(name) + 1}/{len(chain)}")
        t0 = time.time()
        try:
            outcome = await eng.run(job_dir, input_path, trace, budget)
        except Exception as exc:  # engine crash must not kill the job
            attempts.append(Attempt(name, False, f"crash: {exc}", time.time() - t0))
            continue
        if not outcome.ok or not outcome.main:
            attempts.append(Attempt(name, False, outcome.note,
                                    time.time() - t0))
            continue
        body = decode(open(outcome.main, "rb").read())
        if 'error("devirt:' in body and _code_lines(body) < 20:
            attempts.append(Attempt(name, False,
                                    "devirt placeholder in output",
                                    time.time() - t0))
            continue
        if _code_lines(body) == 0:
            attempts.append(Attempt(name, False, "no code lines",
                                    time.time() - t0))
            continue
        if len(raw) > 262144 and _code_lines(body) < 20:
            attempts.append(Attempt(name, False,
                                    "truncated: large input, "
                                    f"{_code_lines(body)} code lines",
                                    time.time() - t0))
            continue
        attempts.append(Attempt(name, True, outcome.note, time.time() - t0))
        await say(100, "artifact ready")
        note = await _syntax_note(outcome.main) or outcome.note
        _relocate(job_dir, outcome)
        result.ok = True
        result.engine = name
        result.main = outcome.main
        result.note = note
        result.partial = outcome.partial
        result.artifacts = list(dict.fromkeys(outcome.artifacts + _gather_extra(job_dir, outcome.main)))
        result.seconds = time.time() - started
        try:
            body = decode(open(outcome.main, "rb").read())
            result.analysis = analyzer.analyze(body)
            await _write_reports(job_dir, result)
        except OSError:
            pass
        return result

    # nothing produced: still analyze whatever the user handed us
    result.analysis = analyzer.analyze(text)
    skipped = [a for a in attempts if a.note.startswith("no matching fingerprint")]
    tried = [a for a in attempts if a not in skipped and not a.ok]
    if det.family and not det.engine:
        result.note = f"detected {det.family}; no engine for it yet"
    elif tried:
        result.note = "tried " + ", ".join(
            f"{a.engine}: {a.note[:70]}" for a in tried)
    elif skipped:
        result.note = (f"detected {det.family}; engines skipped on fingerprint "
                       f"gate ({len(skipped)})")
    else:
        result.note = "no obfuscator signature and no engine produced output"
    result.seconds = time.time() - started
    result.stdout = "\n".join(
        f"{a.engine}: {'ok' if a.ok else a.note}" for a in attempts)
    await _write_reports(job_dir, result)
    return result


async def _write_reports(job_dir: str, result: JobResult) -> None:
    if result.analysis:
        with open(os.path.join(job_dir, "analysis.txt"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(f"engine: {result.engine or 'none'}\n")
            f.write(f"detection: {result.detection.engine or 'unknown'} "
                    f"{result.detection.confidence:.2f}\n")
            f.write(f"trace: {result.trace}\n\n")
            f.write(result.analysis.render() + "\n")
        p = os.path.join(job_dir, "analysis.txt")
        if p not in result.artifacts:
            result.artifacts.append(p)

    meta = {
        "engine": result.engine,
        "trace": result.trace,
        "detection": {
            "engine": result.detection.engine,
            "confidence": round(result.detection.confidence, 2),
            "evidence": result.detection.evidence,
        },
        "risk": result.analysis.risk if result.analysis else None,
        "partial": result.partial,
        "seconds": round(result.seconds, 1),
        "attempts": [
            {"engine": a.engine, "ok": a.ok, "note": a.note,
             "seconds": round(a.seconds, 1)} for a in result.attempts
        ],
        "main": os.path.basename(result.main) if result.main else None,
    }
    with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

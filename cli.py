#!/usr/bin/env python3
"""CLI twin of the bot: same pipeline, no Discord.

    python cli.py script.lua
    python cli.py script.lua --trace
    python cli.py https://example.com/loader.lua --get
    python cli.py --engines
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import analyzer, config, detector, downloader, pipeline
from core.engines import ENGINES, ORDER


def _print_analysis(res: pipeline.JobResult) -> None:
    print(f"engine   : {res.engine or 'none'}")
    det = res.detection
    print(f"detected : {det.engine or 'unknown'} ({det.confidence:.2f})")
    if det.evidence:
        print("evidence : " + "; ".join(det.evidence[:4]))
    print(f"time     : {res.seconds:.1f}s")
    if res.partial:
        print("partial  : engine hit an anti-tamper gate")
    if res.note and not res.ok:
        print(f"note     : {res.note}")
    if res.analysis:
        print(f"risk     : {res.analysis.risk}  "
              f"({res.analysis.lines} lines, {res.analysis.bytes} bytes)")
        for f in res.analysis.findings:
            print(f"  [{f.severity}] {f.label} x{f.count}")
            for s in f.samples[:2]:
                print("      " + s)


async def _main(args: argparse.Namespace) -> int:
    if args.engines:
        for eng in ORDER:
            ok, why = eng.available()
            print(f"{'ok  ' if ok else 'MISS'} {eng.name:<10} {eng.label}"
                  + ("" if ok else f"  ({why})"))
        return 0

    src = args.source
    job_dir = pipeline.new_workdir()
    url = None
    if args.get or downloader.find_url(src):
        url = downloader.find_url(src) or src
        data, name = await downloader.fetch(url)
    else:
        with open(src, "rb") as f:
            data = f.read()
        name = os.path.basename(src)
    path, name = downloader.prepare_script(data, name, job_dir)

    if args.detect_only:
        det = detector.detect(pipeline.decode(open(path, "rb").read()))
        print(f"engine   : {det.engine or 'unknown'}")
        print(f"score    : {det.confidence:.2f}")
        print("evidence : " + ("; ".join(det.evidence) or "none"))
        return 0

    res = await pipeline.run_job(path, trace=args.trace,
                                 force_engine=args.engine)
    _print_analysis(res)
    if not res.ok:
        print("\nno output produced. workdir: " + job_dir)
        return 1

    out = args.out or os.path.dirname(os.path.abspath(src))
    os.makedirs(out, exist_ok=True)
    stem = os.path.splitext(name)[0]
    target = os.path.join(out, f"{stem}.deobf.lua")
    with open(res.main, "rb") as rf, open(target, "wb") as wf:
        wf.write(rf.read())
    print(f"\noutput   : {target}")
    for art in res.artifacts:
        base = os.path.basename(art)
        if base in ("strings.txt", "analysis.txt", "report.json", "job.json"):
            dst = os.path.join(out, f"{stem}.{base}")
            with open(art, "rb") as rf, open(dst, "wb") as wf:
                wf.write(rf.read())
            print(f"artifact : {dst}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="deobf", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="file path or URL")
    ap.add_argument("--get", action="store_true", help="treat source as a URL")
    ap.add_argument("--engine", choices=list(ENGINES), help="force one engine")
    ap.add_argument("--trace", action="store_true", help="trace mode only")
    ap.add_argument("--detect", dest="detect_only", action="store_true",
                    help="fingerprint only")
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--engines", action="store_true", help="list engines")
    args = ap.parse_args()
    if not args.engines and not args.source:
        ap.print_help()
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

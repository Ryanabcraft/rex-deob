#!/usr/bin/env python3
"""Run every family sample through the pipeline and print a matrix.

    python tests/run_corpus.py            # all
    python tests/run_corpus.py -f wearedevs
    python tests/run_corpus.py --write    # also dump tests/report.json

Exit code 1 when a sample with an expected engine did not produce output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import pipeline  # noqa: E402

CORPUS = os.path.join(HERE, "corpus")
MANIFEST = os.path.join(HERE, "corpus_manifest.json")
FALLBACK = {"strings", "unveilr"}


def load_manifest() -> dict:
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


async def run_one(family: str, path: str, expect: str | None) -> dict:
    t0 = time.time()
    res = await pipeline.run_job(path)
    row = {
        "family": family,
        "file": os.path.basename(path),
        "expect": expect,
        "engine": res.engine,
        "ok": res.ok,
        "partial": res.partial,
        "seconds": round(time.time() - t0, 1),
        "lines": res.analysis.lines if res.analysis else 0,
        "bytes": res.analysis.bytes if res.analysis else 0,
        "quality": round(res.quality.score, 3) if res.quality else None,
        "note": (res.note or "")[:160],
    }
    if expect is None:
        row["verdict"] = "GAP" if not res.ok else "OK (bonus)"
    elif res.ok and res.engine == expect:
        row["verdict"] = "PASS"
    elif res.ok and res.engine in FALLBACK:
        # strings/unveilr are behaviour or literal dumps, not recovered source.
        row["verdict"] = f"GAP ({res.engine})"
    elif res.ok:
        row["verdict"] = f"PASS* ({res.engine})"
    else:
        row["verdict"] = "FAIL"
    return row


async def main(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    rows: list[dict] = []
    items: list[tuple[str, str, str | None]] = []
    for family, cfg in manifest.items():
        folder = os.path.join(CORPUS, family)
        if not os.path.isdir(folder):
            continue
        if args.family and family not in args.family:
            continue
        overrides = cfg.get("files") or {}
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".lua", ".luau", ".txt", ".lua5")):
                continue
            expect = overrides[name] if name in overrides else cfg.get("engine")
            items.append((family, os.path.join(folder, name), expect))

    print(f"{'family':<14} {'file':<34} {'verdict':<16} {'engine':<12} "
          f"{'s':>6} {'lines':>6} {'q':>5}")
    print("-" * 108)
    for family, path, expect in items:
        row = await run_one(family, path, expect)
        rows.append(row)
        q = row["quality"]
        print(f"{row['family']:<14} {row['file'][:34]:<34} {row['verdict']:<16} "
              f"{str(row['engine']):<12} {row['seconds']:>6} {row['lines']:>6} "
              f"{(f'{q:.2f}' if q is not None else '-'):>5}")

    fails = [r for r in rows if r["verdict"] == "FAIL"]
    gaps = [r for r in rows if r["verdict"].startswith("GAP")]
    passes = [r for r in rows if r["verdict"].startswith(("PASS", "OK"))]
    print("-" * 100)
    print(f"pass {len(passes)}  fail {len(fails)}  gap {len(gaps)}  total {len(rows)}")
    for r in fails:
        print(f"  FAIL {r['family']}/{r['file']}: {r['note']}")
    for r in gaps:
        print(f"  GAP  {r['family']}/{r['file']}")

    if args.write:
        out = os.path.join(HERE, "report.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print("report: " + out)
    return 0 if not fails else 1


def cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-f", "--family", action="append")
    ap.add_argument("--write", action="store_true")
    return asyncio.run(main(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(cli())

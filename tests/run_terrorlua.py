#!/usr/bin/env python3
"""Download small samples from terrorlua/obfuscator-samples and test every family.

    python tests/run_terrorlua.py --per-family 2 --max-kb 120

Uses the authenticated `gh` CLI for listing and raw.githubusercontent for the
files themselves. Results go to tests/terrorlua_report.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import pipeline  # noqa: E402

REPO = "terrorlua/obfuscator-samples"
OUT = os.path.join(HERE, "terrorlua")

EXPECT = {
    "77fuscator": "f77", "Boronide": None, "Hercules": "hercules",
    "IronBrew2": "ib2net", "Ironbrew1": "ib1", "Ironbrew3": None,
    "LPS": None, "LuaObfuscator": "luaobfuscator", "Luraph": "luraph15",
    "MoonSec": "moonsec", "MoonVeil": None, "PSU": "psu",
    "Prometheus": "prometheus", "SynapseXen": "synapsexen",
    "wYnFuscate": "wyn", "Clyde": None,
}


def gh(path: str):
    res = subprocess.run(
        ["gh", "api", f"repos/{REPO}/contents/{path}"],
        capture_output=True, text=True)
    if res.returncode != 0:
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def walk(path: str, depth: int, acc: list) -> None:
    if depth < 0:
        return
    for item in gh(path):
        if item.get("type") == "dir":
            walk(item["path"], depth - 1, acc)
        elif item.get("name", "").lower().endswith((".lua", ".luau", ".txt")):
            acc.append((item["path"], item.get("size", 0)))


def download(path: str, dest: str) -> bool:
    url = f"https://raw.githubusercontent.com/{REPO}/HEAD/{path}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


async def run_one(fam: str, path: str, expect: str | None) -> dict:
    res = await pipeline.run_job(path, max_engine_seconds=90,
                                 skip_engines={"unveilr", "strings"})
    row = {
        "family": fam,
        "file": os.path.basename(path),
        "engine": res.engine,
        "ok": res.ok,
        "partial": res.partial,
        "lines": res.analysis.lines if res.analysis else 0,
        "seconds": round(res.seconds, 1),
        "note": (res.note or "")[:140],
        "expect": expect,
    }
    if not res.ok:
        # expect=None means "no engine claims this family yet" — a gap, not a
        # regression. Only an expected engine failing counts as FAIL.
        row["verdict"] = "GAP" if expect is None else "FAIL"
    elif res.partial:
        row["verdict"] = "PARTIAL"
    else:
        row["verdict"] = "PASS"
    return row


async def main(args: argparse.Namespace) -> int:
    fams = [i["name"] for i in gh("") if i.get("type") == "dir"]
    print("families:", ", ".join(fams))
    rows: list[dict] = []
    for fam in fams:
        files: list[tuple[str, int]] = []
        walk(fam, 3, files)
        files = [f for f in files if 300 < f[1] <= args.max_kb * 1024]
        files.sort(key=lambda f: f[1], reverse=True)
        pick = files[:args.per_family]
        print(f"\n== {fam}: {len(files)} candidates, picking {len(pick)}")
        for path, size in pick:
            local = os.path.join(OUT, fam, path.split("/", 1)[1].replace("/", "__"))
            if not os.path.exists(local) and not download(path, local):
                print("   download failed:", path)
                continue
            expect = EXPECT.get(fam)
            row = await run_one(fam, local, expect)
            rows.append(row)
            print(f"   {row['verdict']:<8} {row['file'][:38]:<38} "
                  f"{str(row['engine']):<10} {row['seconds']:>6}s "
                  f"{row['lines']:>6}L")
    print("\n" + "=" * 70)
    by_fam: dict[str, list[dict]] = {}
    for r in rows:
        by_fam.setdefault(r["family"], []).append(r)
    total = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "GAP": 0}
    for fam, rs in by_fam.items():
        c = {k: sum(1 for r in rs if r["verdict"] == k) for k in total}
        for k in total:
            total[k] += c[k]
        print(f"{fam:<16} pass {c['PASS']}  partial {c['PARTIAL']}  "
              f"fail {c['FAIL']}  gap {c['GAP']}")
    print("-" * 70)
    print(f"TOTAL pass {total['PASS']}  partial {total['PARTIAL']}  "
          f"fail {total['FAIL']}  gap {total['GAP']}  samples {len(rows)}")
    with open(os.path.join(HERE, "terrorlua_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=2)
    ap.add_argument("--max-kb", type=int, default=120)
    raise SystemExit(asyncio.run(main(ap.parse_args())))

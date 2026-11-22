import argparse
import os
import sys

from .fetch import Fetcher, FetchError
from .layers import DeobfError
from .pipeline import deobfuscate


def parse_header(text):
    if ":" not in text:
        raise argparse.ArgumentTypeError("header must look like 'Name: value'")
    k, v = text.split(":", 1)
    return k.strip(), v.strip()


def stage_path(base, index):
    if index == 0:
        return base
    root, ext = os.path.splitext(base)
    return "%s.stage%d%s" % (root, index + 1, ext or ".lua")


def build_parser():
    p = argparse.ArgumentParser(prog="orrxl4", description="Deobfuscator for Orrxl4 protected Lua scripts")
    p.add_argument("target", help="protector URL, protector id, or a local file")
    p.add_argument("-o", "--output", default="deobfuscated.lua", help="where to write the result")
    p.add_argument("--follow", action="store_true", help="also fetch and deobfuscate the script a loadstring(game:HttpGet(url)) payload points to")
    p.add_argument("--keep-names", action="store_true", help="do not rename obfuscated identifiers")
    p.add_argument("--raw", action="store_true", help="write the exact decoded code without reconstruction")
    p.add_argument("--save-fetched", metavar="PATH", help="store the fetched protected loader")
    p.add_argument("--stdout", action="store_true", help="print the result instead of writing a file")
    p.add_argument("--timeout", type=float, default=300.0, help="sandbox time limit per layer in seconds")
    p.add_argument("--max-layers", type=int, default=64)
    p.add_argument("--no-fast", action="store_true", help="interpret every function instead of using verified native shortcuts")
    p.add_argument("--retries", type=int, default=45, help="fetch attempts before giving up")
    p.add_argument("--delay", type=float, default=0.7, help="seconds between fetch attempts")
    p.add_argument("--user-agent", help="user agent tried first when fetching")
    p.add_argument("--header", action="append", type=parse_header, default=[], help="extra request header, repeatable")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    def log(msg):
        print(msg, file=sys.stderr)

    chatter = log if args.verbose else None
    fetcher = Fetcher(
        retries=args.retries,
        delay=args.delay,
        user_agent=args.user_agent,
        headers=dict(args.header),
        log=chatter,
    )
    try:
        results = deobfuscate(
            args.target,
            fetcher=fetcher,
            rename=not args.keep_names,
            timeout=args.timeout,
            max_layers=args.max_layers,
            follow=args.follow,
            log=chatter,
            fast=not args.no_fast,
        )
    except FetchError as e:
        log("fetch failed: %s" % e)
        return 2
    except DeobfError as e:
        log("deobfuscation failed: %s" % e)
        return 3
    if args.save_fetched:
        with open(args.save_fetched, "wb") as fh:
            fh.write(results[0].raw)
    for i, r in enumerate(results):
        payload = r.final if args.raw else r.clean
        if args.stdout:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.write(b"\n")
        else:
            path = stage_path(args.output, i)
            with open(path, "wb") as fh:
                fh.write(payload)
            log("stage %d: %s -> %s (%d layer%s)" % (i + 1, r.origin, path, len(r.layers), "" if len(r.layers) == 1 else "s"))
        for u in r.urls:
            log("  url: %s" % u)
    return 0

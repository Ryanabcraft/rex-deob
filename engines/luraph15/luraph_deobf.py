#!/usr/bin/env python3
"""Luraph v15 / v15.1 deobfuscator -- one-command front end.

    python3 luraph_deobf.py script.lua                 -> output/script.deobf.lua
    python3 luraph_deobf.py script.lua -o clean.lua
    python3 luraph_deobf.py pasta_com_scripts/          (every .lua / .luau / .txt)
    python3 luraph_deobf.py script.lua --trace          (fast behaviour trace only)
    python3 luraph_deobf.py script.lua --game-id 1686885941 --place-id 4924922222
    python3 luraph_deobf.py script.lua --raw            (skip junk cleanup / polish)

Pipeline: node deob.js (sandbox run + live constant decryption + VM lifting)
          -> luaclean.py (removes provably dead Luraph junk)
          -> polish.py   (Connect folding, Color3.fromRGB, unused runtime closures)
Every stage after the lift re-parses its output with luau-ast and keeps the
previous text if anything fails, so the result always compiles.
Any unknown option is passed straight to deob.js (see README / TECHNICAL.md).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WIN = os.name == "nt"
LUAU_AST = os.path.join(HERE, "bin", "luau-ast.exe" if WIN else "luau-ast")
LUAU = os.path.join(HERE, "bin", "luau.exe" if WIN else "luau")
EXTS = (".lua", ".luau", ".txt")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def check_env():
    if not shutil.which("node"):
        sys.exit("[!] Node.js 18+ nao encontrado (instale de https://nodejs.org)")
    for b in (LUAU, LUAU_AST):
        if not os.path.exists(b):
            sys.exit("[!] faltando %s (rode: python3 build_luau.py)" % b)
        if not WIN and not os.access(b, os.X_OK):
            os.chmod(b, 0o755)


def run_stage(cmd, env=None, timeout=None):
    """Run one stage. A stage that hangs (luau-ast on a huge file) must not eat the
    whole job: it is killed at `timeout` and the previous text is kept, so a slow
    polish can never cost the lift that already succeeded."""
    try:
        r = subprocess.run(cmd, cwd=HERE, env=env, stdout=sys.stderr, stderr=sys.stderr,
                           timeout=timeout)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        log("[!] etapa cancelada: passou de %ss (%s)" % (timeout, os.path.basename(str(cmd[1]))))
        return False


def deobf_one(src, dst, opts, passthrough):
    t0 = time.time()
    log("\n=== %s" % src)
    work = tempfile.mkdtemp(prefix="luraph_")
    try:
        base = os.path.splitext(os.path.basename(src))[0]
        if base.endswith(".lua"):
            base = base[:-4]
        tin = os.path.join(work, base + ".lua")
        shutil.copyfile(src, tin)
        raw = os.path.join(work, base + ".raw.lua")
        cmd = ["node", os.path.join(HERE, "deob.js"), tin, "-o", raw]
        if opts.trace:
            cmd.append("--no-devirt")
        if opts.raw:
            cmd.append("--raw")
        cfg = {}
        if opts.game_id:
            cfg["__superz_game_id"] = opts.game_id
        if opts.place_id:
            cfg["__superz_place_id"] = opts.place_id
        if cfg:
            cmd += ["--cfg-json", json.dumps(cfg)]
        cmd += ["--timeout", str(opts.timeout)]
        cmd += passthrough
        env = dict(os.environ)
        env.setdefault("PYTHON_BIN", sys.executable or "python3")
        if not run_stage(cmd, env, timeout=opts.timeout + 60) or not os.path.exists(raw):
            log("[!] falhou: %s" % src)
            return False
        cur = raw
        # Polish already happened inside deob.js (polish_engine.py + polish2.py,
        # the strongest chain, with its own 25%-loss guard and DEOB_POLISH_TIMEOUT).
        # Running luaclean.py + polish.py again here only re-parsed and rewrote the
        # same 70KB a second time -- and could undo the engine's renames.
        # --raw skips it inside deob.js; the file is copied as the lifter wrote it.
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        shutil.copyfile(cur, dst)
        # camada de leitura: assets fora do corpo, alias inlinado, nomes de servico,
        # rotulo/indice por funcao. Texto puro -- se falhar, o arquivo fica igual.
        if os.environ.get("DEOB_READABILITY") != "0":
            try:
                sys.path.insert(0, os.path.join(HERE, "core"))
                import readability
                before = open(dst, encoding="utf-8", errors="replace").read()
                after, stats = readability.improve(before, os.path.splitext(os.path.abspath(dst))[0] + ".assets")
                if after != before:
                    with open(dst, "w", encoding="utf-8") as fh:
                        fh.write(after)
                log("[+] legibilidade: " + readability.describe(stats))
            except Exception as exc:
                log("[!] legibilidade ignorada: %s" % exc)
        text = open(dst, encoding="utf-8", errors="replace").read()
        markers = text.count('error("devirt:')
        undecoded = text.count("constant not decoded")
        log("[+] %s  (%d linhas, %.0fs)" % (dst, text.count("\n") + 1, time.time() - t0))
        if markers or undecoded:
            log("[i] %d bloco(s) nao recuperado(s), %d constante(s) nao decodificada(s) -- "
                "rode de novo ou veja SETUP.md (scripts travados por GameId)" % (markers, undecoded))
        return True
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Luraph v15/v15.1 deobfuscator", add_help=True)
    ap.add_argument("input", help="arquivo .lua ou pasta")
    ap.add_argument("-o", "--output", help="arquivo de saida (so com 1 entrada)")
    ap.add_argument("--trace", action="store_true", help="so o trace de comportamento (rapido)")
    ap.add_argument("--raw", action="store_true", help="nao limpar/polir a saida do lifter")
    ap.add_argument("--game-id", type=int, help="GameId (universe id) p/ scripts travados por jogo")
    ap.add_argument("--place-id", type=int, help="PlaceId p/ scripts travados por jogo")
    ap.add_argument("--timeout", type=int, default=300, help="tempo maximo por execucao (s)")
    opts, passthrough = ap.parse_known_args()
    check_env()

    if os.path.isdir(opts.input):
        files = sorted(os.path.join(opts.input, f) for f in os.listdir(opts.input)
                       if f.lower().endswith(EXTS) and ".deobf." not in f)
        outdir = os.path.join(opts.input, "output")
        jobs = [(f, os.path.join(outdir, os.path.splitext(os.path.basename(f))[0].replace(".lua", "") + ".deobf.lua"))
                for f in files]
    else:
        name = os.path.splitext(os.path.basename(opts.input))[0].replace(".lua", "")
        dst = opts.output or os.path.join(os.path.dirname(os.path.abspath(opts.input)), "output", name + ".deobf.lua")
        jobs = [(opts.input, dst)]
    ok = sum(1 for s, d in jobs if deobf_one(s, d, opts, passthrough))
    log("\n[*] pronto: %d de %d ok" % (ok, len(jobs)))
    sys.exit(0 if ok == len(jobs) else 1)


if __name__ == "__main__":
    main()
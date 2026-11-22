"""Engine adapters: one class per deobfuscator, same async interface.

Every engine runs as a subprocess, writes its log next to the job, and returns
the artifacts it produced. Nothing here knows about Discord.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field

from . import config


CREATE_NO_WINDOW = 0x08000000 if config.IS_WIN else 0


@dataclass
class RunOutcome:
    ok: bool
    engine: str
    main: str | None = None
    artifacts: list[str] = field(default_factory=list)
    note: str = ""
    partial: bool = False
    extra: dict = field(default_factory=dict)


class EngineError(Exception):
    pass


def _env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    env["PYTHON_BIN"] = config.PYTHON
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    if extra:
        env.update(extra)
    return env


async def _run(cmd: list[str], cwd: str, log_path: str, timeout: int,
               env: dict | None = None, quiet: bool = False) -> tuple[int, bool]:
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        kwargs = {}
        if config.IS_WIN:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env or _env(),
            stdout=asyncio.subprocess.DEVNULL if quiet else log,
            stderr=asyncio.subprocess.STDOUT if not quiet else asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, **kwargs,
        )
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        log.write(f"\n[exit={proc.returncode} timed_out={timed_out}]\n")
        return (proc.returncode if proc.returncode is not None else -1), timed_out


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if config.IS_WIN:
            os.system(f"taskkill /F /T /PID {proc.pid} >NUL 2>&1")
        else:
            proc.kill()
    except Exception:
        pass


def _read(path: str, limit: int = 4 * 1024 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _prom_shape(text: str) -> bool:
    from .detector import is_prometheus_shape
    return is_prometheus_shape(text)


class Engine:
    name = ""
    label = ""
    order = 50
    timeout_key = ""
    families: tuple[str, ...] = ()
    can_trace = False

    def available(self) -> tuple[bool, str]:
        return True, ""

    async def run(self, job_dir: str, input_path: str, trace: bool,
                  timeout: int) -> RunOutcome:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _tail(log_path: str, n: int = 6) -> str:
        text = _read(log_path)
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not ln.startswith(("$", "[exit="))]
        return " | ".join(lines[-n:])[-900:]


class Bytecode(Engine):
    """Standalone .luac / Luau bytecode decompiler (Rust, luadec)."""

    name = "bytecode"
    label = "Lua/Luau bytecode (.luac)"
    order = 5
    timeout_key = "bytecode"
    families = ("Lua/Luau bytecode",)

    def __init__(self) -> None:
        self.exe = config.tool("luadec")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(self.exe):
            return False, "missing tools/luadec.exe"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "luadec.log")
        flag = "--flatten" if trace else "--deobfuscate"
        cmd = [self.exe, flag, input_path, out]
        code, to = await _run(cmd, job_dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 16
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        from .cleaners import clean_luadec
        text = clean_luadec(_read(out))
        if text.strip():
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        return RunOutcome(True, self.name, out, [out], extra={"log": log})


class Luraph15(Engine):
    name = "luraph15"
    label = "Luraph v15 / v15.1"
    order = 10
    timeout_key = "luraph15"
    can_trace = True
    families = ("Luraph v15 header", "LPH VM macros", "Luraph VM dispatch shape")

    def __init__(self) -> None:
        self.dir = config.engine("luraph15")

    def available(self) -> tuple[bool, str]:
        if not shutil.which("node"):
            return False, "node not found"
        for b in ("luau.exe", "luau-ast.exe"):
            if not os.path.exists(os.path.join(self.dir, "bin", b)):
                return False, f"missing bin/{b}"
        if not os.path.exists(os.path.join(self.dir, "luraph_deobf.py")):
            return False, "missing luraph_deobf.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "luraph15.log")
        cmd = [config.PYTHON, "luraph_deobf.py", input_path, "-o", out,
               "--timeout", str(max(120, timeout - 120))]
        if trace:
            cmd.append("--trace")
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        artifacts = [out] if ok else []
        for extra in (out + ".strings.txt", out + ".report.json"):
            if os.path.exists(extra):
                artifacts.append(extra)
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        note = "trace only (no devirtualization)" if trace else ""
        partial = False
        head = _read(out, 4096)
        m = re.search(r"recovery:\s*(\d+)%[^\n]*?(\d+)\s*blocks unlifted", head)
        if m and int(m.group(1)) < 100:
            partial = True
            note = (f"lift {m.group(1)}% · {m.group(2)} unlifted blocks "
                    f"kept as error() placeholders")
        return RunOutcome(True, self.name, out, artifacts, note,
                          partial=partial, extra={"log": log})


class Ib1(Engine):
    """IronBrew1 static recovery: serialized payload -> IR + full constants."""

    name = "ib1"
    label = "IronBrew 1 (IR + constants)"
    order = 19
    timeout_key = "ib1"
    families = ("IronBrew1 header", "IronBrew signature")

    def __init__(self) -> None:
        self.dir = config.engine("ib1")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "ib1.py")):
            return False, "missing ib1.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        outdir = os.path.join(job_dir, "ib1")
        os.makedirs(outdir, exist_ok=True)
        log = os.path.join(job_dir, "ib1.log")
        cmd = [config.PYTHON, "ib1.py", "all", input_path, "-o", outdir]
        await _run(cmd, self.dir, log, timeout)
        main = os.path.join(outdir, "recovered.lua")
        if not (os.path.exists(main) and os.path.getsize(main) > 64):
            return RunOutcome(False, self.name, note=self._tail(log))
        arts = [main]
        for name in ("constants.txt", "compare.txt", "compare.json"):
            p = os.path.join(outdir, name)
            if os.path.exists(p):
                arts.append(p)
        return RunOutcome(True, self.name, main, arts,
                          "IR reconstruction: virtual opcodes preserved, "
                          "constants fully recovered", partial=True,
                          extra={"log": log})


class Luraph14(Engine):
    name = "luraph14"
    label = "Luraph v14 / IronBrew / generic"
    order = 20
    timeout_key = "luraph14"
    can_trace = True
    families = ("Luraph v14 header", "IronBrew1 header", "IronBrew signature")

    def __init__(self) -> None:
        self.dir = config.engine("luraph14")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "deobf", "deob.py")):
            return False, "missing deobf/deob.py"
        if not os.path.exists(os.path.join(self.dir, "deobf", "bin", "luau.exe")):
            return False, "missing deobf/bin/luau.exe"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "luraph14.log")
        cmd = [config.PYTHON, os.path.join("deobf", "deob.py"), input_path, "-o", out]
        if trace:
            cmd.append("--no-devirt")
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out],
                          "trace only (no devirtualization)" if trace else "",
                          extra={"log": log})


class Prometheus(Engine):
    """Prometheus / WeAreDevs: pdeobf → wad dumper → promjs → leakd dumper."""

    name = "prometheus"
    label = "Prometheus / WeAreDevs"
    order = 30
    timeout_key = "prometheus"

    def __init__(self) -> None:
        self.dir = config.engine("prometheus")
        self.wad = config.engine("promwad")
        self.js = config.engine("promjs", "promdeobf")
        self.dumper = config.engine("promdumper")

    def available(self) -> tuple[bool, str]:
        if not shutil.which("node"):
            return False, "node not found"
        if not os.path.exists(os.path.join(self.dir, "bin", "pdeobf.js")):
            return False, "missing bin/pdeobf.js"
        return True, ""

    async def identify(self, input_path: str) -> dict | None:
        log = os.path.join(os.path.dirname(input_path), "prom_detect.log")
        cmd = ["node", os.path.join("bin", "pdeobf.js"), input_path,
               "-o", os.path.join(os.path.dirname(input_path), ".prom_probe.lua"), "-q"]
        code, to = await _run(cmd, self.dir, log, 90)
        text = _read(log)
        return {"prometheus": "not prometheus" not in text and code == 0,
                "tail": self._tail(log, 4)}

    @staticmethod
    def _code_lines(text: str) -> int:
        return sum(1 for ln in text.splitlines()
                   if ln.strip() and not ln.strip().startswith("--"))

    @staticmethod
    def _payload(text: str) -> str:
        """wad output: drop the decoded `local Constants` dump, keep the code."""
        lines = text.splitlines()
        cut = 0
        for i, ln in enumerate(lines):
            if ln.strip().startswith("local Constants = {"):
                cut = i + 1
        return "\n".join(lines[cut:]).lstrip("\n")

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        if trace:
            return RunOutcome(False, self.name,
                              note="Prometheus engine has no trace-only mode")
        head = _read(input_path, 65536)
        force = ("wearedevs.net/obfuscator" in head or "Prometheus" in head
                 or _prom_shape(head))
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "prometheus.log")
        notes: list[str] = []

        # 1) pdeobf — reconstructed source with inferred names
        for forcar in ((False, True) if force else (False,)):
            if os.path.exists(out):
                os.remove(out)
            cmd = ["node", os.path.join("bin", "pdeobf.js"), input_path, "-o", out]
            if forcar:
                cmd.append("--force")
            await _run(cmd, self.dir, log, timeout)
            if os.path.exists(out) and os.path.getsize(out) > 16:
                from .cleaners import clean_pdeobf
                text = clean_pdeobf(_read(out))
                if self._code_lines(text):
                    with open(out, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                    note = "" if self._code_lines(text) > 1 else ""
                    return RunOutcome(True, self.name, out, [out], note,
                                      extra={"log": log})
            notes.append("pdeobf" + ("+force" if forcar else ""))

        # 2) wad dumper — emulates the environment
        if os.path.exists(os.path.join(self.wad, "deobfuscator.py")):
            local = os.path.join(job_dir, "wad_in.lua")
            shutil.copyfile(input_path, local)
            await _run([config.PYTHON, "deobfuscator.py", local],
                       self.wad, log, timeout)
            produced = local + ".deobf.lua"
            if os.path.exists(produced):
                body = self._payload(_read(produced))
                if self._code_lines(body):
                    with open(out, "w", encoding="utf-8", newline="\n") as f:
                        f.write(body)
                    return RunOutcome(True, self.name, out, [out],
                                      extra={"log": log})
            notes.append("wad")

        # 3) prometheus-js — decrypted logic with obfuscator names
        main_js = os.path.join(self.js, "main.js")
        if os.path.exists(main_js):
            if os.path.exists(out):
                os.remove(out)
            await _run(["node", "main.js", input_path, out],
                       self.js, log, timeout)
            if os.path.exists(out) and os.path.getsize(out) > 32:
                return RunOutcome(True, self.name, out, [out],
                                  "decrypted logic; names are the obfuscator's",
                                  extra={"log": log})
            notes.append("promjs")

        # 4) leakd dumper — captures chunks, good for hidden webhooks
        if os.path.exists(os.path.join(self.dumper, "deobfuscator.py")):
            local = os.path.join(job_dir, "dump_in.lua")
            shutil.copyfile(input_path, local)
            await _run([config.PYTHON, "deobfuscator.py", local],
                       self.dumper, log, timeout)
            produced = local + ".deobf.lua"
            if os.path.exists(produced) and os.path.getsize(produced) > 16:
                shutil.copyfile(produced, out)
                arts = [out]
                rep = local + ".report.txt"
                if os.path.exists(rep):
                    arts.append(rep)
                return RunOutcome(True, self.name, out, arts,
                                  "chunk dump: partial view, strings captured",
                                  partial=True, extra={"log": log})
            notes.append("leakd")

        return RunOutcome(False, self.name,
                          note="all strategies failed: " + ", ".join(notes)
                          + " | " + self._tail(log))


class Moonsec(Engine):
    name = "moonsec"
    label = "MoonSec V3"
    order = 40
    timeout_key = "moonsec"
    can_trace = True
    families = ("MoonSec V3 watermark",)

    def __init__(self) -> None:
        self.dir = config.engine("moonsec")
        self.exe = os.path.join(self.dir, "bin", "MoonsecDeobfuscator.exe")
        self.jar = os.path.join(self.dir, "unluac.jar")
        self.lua = os.path.join(self.dir, "lua", "lua5.1.exe")
        self.decom = os.path.join(self.dir, "decom.lua")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(self.exe):
            return False, "missing MoonsecDeobfuscator.exe"
        if not (shutil.which("java") or os.path.exists(self.lua)):
            return False, "needs java or bundled lua5.1"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        log = os.path.join(job_dir, "moonsec.log")
        if trace:
            dis = os.path.join(job_dir, "disassembly.txt")
            cmd = [self.exe, "-dis", "-i", input_path, "-o", dis]
            code, to = await _run(cmd, self.dir, log, timeout)
            ok = os.path.exists(dis) and os.path.getsize(dis) > 32
            return RunOutcome(ok, self.name, dis if ok else None,
                              [dis] if ok else [],
                              "" if ok else self._tail(log),
                              extra={"log": log})
        luac = os.path.join(job_dir, "deobf.luac")
        cmd = [self.exe, "-dev", "-i", input_path, "-o", luac]
        code, to = await _run(cmd, self.dir, log, timeout)
        if not os.path.exists(luac) or os.path.getsize(luac) == 0:
            return RunOutcome(False, self.name, note=self._tail(log))
        artifacts = [luac]
        main = None
        if not trace:
            main = os.path.join(job_dir, "deobf.lua")
            rust = config.tool("luadec")
            if os.path.exists(rust):
                await _run([rust, "--deobfuscate", luac, main],
                           job_dir, log, timeout)
            java = shutil.which("java")
            if not (os.path.exists(main) and os.path.getsize(main) > 16) and java:
                await _run([java, "-jar", self.jar, luac, "--output", main],
                           job_dir, log, timeout)
            if not (os.path.exists(main) and os.path.getsize(main) > 16):
                if os.path.exists(self.lua) and os.path.exists(self.decom):
                    await _run([self.lua, self.decom, luac, main],
                               self.dir, log, timeout)
            if os.path.exists(main) and os.path.getsize(main) > 16:
                from .cleaners import clean_luadec
                text = clean_luadec(_read(main))
                if text.strip():
                    with open(main, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                artifacts.append(main)
            else:
                main = None
        ok = bool(main) or trace
        note = ""
        if not main and not trace:
            note = "bytecode dumped, decompiler failed: " + self._tail(log, 6)
        return RunOutcome(ok, self.name, main, artifacts, note,
                          extra={"log": log, "luac": luac})


class Ironbrew2(Engine):
    """IronBrew 2.7.x / AztupBrew 2.7.2 (axomtools), single-layout parser."""

    name = "ironbrew2"
    label = "IronBrew 2.7 / AztupBrew"
    order = 25
    timeout_key = "ironbrew2"
    families = ("IronBrew2 VM prelude",)

    DETECT = (
        r"return table\.concat[_\-a-zA-Z0-9]+",
        r"return [_\-a-zA-Z0-9]+true, ?\{\}, ?[_\-a-zA-Z0-9]+\(\);?",
        r"bit and bit\.bxor or function[_\-a-zA-Z0-9]+, ?[_\-a-zA-Z0-9]+",
        r"local [_\-a-zA-Z0-9]+ = .*table\.concat",
    )

    def __init__(self) -> None:
        self.dir = config.engine("ironbrew2")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "processor.py")):
            return False, "missing processor.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        if trace:
            return RunOutcome(False, self.name, note="no trace mode")
        import re
        head = _read(input_path, 2 * 1024 * 1024)
        if not any(re.search(p, head) for p in self.DETECT):
            return RunOutcome(False, self.name,
                              note="unrecognised IronBrew2 build "
                                   "(supported: 2.7.0-2.7.2 / AztupBrew)")
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "ironbrew2.log")
        cmd = [config.PYTHON, "processor.py", input_path, out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out], extra={"log": log})


class LuaObfuscator(Engine):
    """Static LuaObfuscator deobfuscator (Chaotic Good / Chaotic Evil)."""

    name = "luaobfuscator"
    label = "LuaObfuscator (Chaotic Good/Evil)"
    order = 35
    timeout_key = "luaobfuscator"

    def __init__(self) -> None:
        self.dir = config.engine("luaobfuscator")

    def available(self) -> tuple[bool, str]:
        if not shutil.which("node"):
            return False, "node not found"
        if not os.path.exists(os.path.join(self.dir, "index.js")):
            return False, "missing index.js"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "luaobfuscator.log")
        cmd = ["node", "index.js", input_path, "--out", out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 16
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out], extra={"log": log})


class Orrxl4(Engine):
    """Orrxl4 loader peeler + interpreter (LLH Lua Land)."""

    name = "orrxl4"
    label = "Orrxl4"
    order = 45
    timeout_key = "orrxl4"
    families = ("Orrxl4 header",)

    def __init__(self) -> None:
        self.dir = config.engine("orrxl4")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "main.py")):
            return False, "missing main.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "orrxl4.log")
        cmd = [config.PYTHON, "main.py", input_path, "-o", out]
        if trace:
            cmd.append("-v")
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 16
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out], extra={"log": log})


class Ib2Net(Engine):
    """IronBrew2 devirtualizer (prebuilt .NET, LuaAnalysis)."""

    name = "ib2net"
    label = "IronBrew 2 (LuaAnalysis)"
    order = 24
    timeout_key = "ib2net"
    families = ()

    def __init__(self) -> None:
        self.dir = config.engine("ib2net")
        self.exe = os.path.join(self.dir, "LuaAnalysis.Ironbrew2.exe")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(self.exe):
            return False, "missing LuaAnalysis.Ironbrew2.exe"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        sub = os.path.join(job_dir, "ib2")
        os.makedirs(sub, exist_ok=True)
        luac = os.path.join(sub, "out.luac")
        log = os.path.join(job_dir, "ib2net.log")
        code, to = await _run([self.exe, input_path, luac], sub, log, timeout)
        src = os.path.join(sub, "output.lua")
        main = None
        if os.path.exists(src) and os.path.getsize(src) > 32:
            main = os.path.join(job_dir, "deobf.lua")
            shutil.copyfile(src, main)
        artifacts = [a for a in (luac, main) if a and os.path.exists(a)]
        ok = main is not None
        note = "" if ok else "no readable source emitted: " + self._tail(log)
        return RunOutcome(ok, self.name, main, artifacts, note,
                          extra={"log": log})


class SynapseXen(Engine):
    """Synapse Xen v1.1.2: decode VM program (partial, calls stay anonymous)."""

    name = "synapsexen"
    label = "Synapse Xen 1.1.2"
    order = 42
    timeout_key = "synapsexen"
    families = ("Synapse Xen",)

    def __init__(self) -> None:
        self.dir = config.engine("synapsexen")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "cli.py")):
            return False, "missing cli.py"
        if not os.path.exists(os.path.join(self.dir, "bin", "lua.exe")):
            return False, "missing bin/lua.exe"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "synapsexen.log")
        env = _env({"PATH": os.path.join(self.dir, "bin") + os.pathsep
                    + os.environ.get("PATH", "")})
        cmd = [config.PYTHON, "cli.py", input_path, "-o", out]
        code, to = await _run(cmd, self.dir, log, timeout, env=env)
        ok = os.path.exists(out) and os.path.getsize(out) > 64
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        text = _read(out)
        code_lines = sum(1 for ln in text.splitlines()
                         if ln.strip() and not ln.strip().startswith("--"))
        if code_lines < 20:
            return RunOutcome(False, self.name,
                              note=f"partial VM only ({code_lines} code lines)")
        return RunOutcome(True, self.name, out, [out],
                          "partial: VM calls recovered without names",
                          partial=True, extra={"log": log})


class Psu(Engine):
    """PSU 4.0/4.5 static devirtualizer (memcpython)."""

    name = "psu"
    label = "PSU 4.0 / 4.5"
    order = 43
    timeout_key = "psu"
    families = ("PSU",)

    def __init__(self) -> None:
        self.dir = config.engine("psu")

    def available(self) -> tuple[bool, str]:
        if not shutil.which("node"):
            return False, "node not found"
        if not os.path.exists(os.path.join(self.dir, "src", "cli.js")):
            return False, "missing src/cli.js"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        raw_out = os.path.join(job_dir, "psu_raw.lua")
        luac = os.path.join(job_dir, "deobf.luac")
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "psu.log")
        cmd = ["node", os.path.join("src", "cli.js"), input_path,
               "-o", raw_out, "--bytecode", luac]
        await _run(cmd, self.dir, log, timeout)
        rust = config.tool("luadec")
        if os.path.exists(luac) and os.path.exists(rust):
            await _run([rust, "--deobfuscate", luac, out], job_dir, log, timeout)
        if not (os.path.exists(out) and os.path.getsize(out) > 64):
            if os.path.exists(raw_out) and os.path.getsize(raw_out) > 64:
                shutil.copyfile(raw_out, out)
        ok = os.path.exists(out) and os.path.getsize(out) > 64
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        arts = [a for a in (out, raw_out, luac) if os.path.exists(a)]
        note = "register-level output" if out == raw_out else ""
        return RunOutcome(True, self.name, out, arts, note,
                          extra={"log": log})


class Hercules(Engine):
    """Hercules 1.6.x-2.0.x static decompiler (pure python)."""

    name = "hercules"
    label = "Hercules v1.6-2.0"
    order = 44
    timeout_key = "hercules"
    can_trace = True
    families = ("Hercules v1.x",)

    def __init__(self) -> None:
        self.dir = config.engine("hercules")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "deobfhercules.py")):
            return False, "missing deobfhercules.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "hercules.log")
        cmd = [config.PYTHON, "deobfhercules.py", input_path, out]
        if trace:
            cmd.append("--mode")
            cmd.append("both")
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        arts = []
        if ok:
            text = _read(out)
            lines = text.splitlines()
            first = 0
            for i, ln in enumerate(lines):
                if ln.strip() and not ln.strip().startswith("--"):
                    first = i
                    break
            header, body = lines[:first], lines[first:]
            if header and body:
                strings_path = os.path.join(job_dir, "strings.txt")
                with open(strings_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(header) + "\n")
                arts.append(strings_path)
                with open(out, "w", encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(body) + "\n")
            arts.insert(0, out)
        asm = out[:-4] + ".asm" if out.endswith(".lua") else out + ".asm"
        if os.path.exists(asm):
            arts.append(asm)
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, arts, extra={"log": log})


class MoonVeil(Engine):
    """MoonVeil 2.1.x analysis toolkit: descrambled container + report."""

    name = "moonveil"
    label = "MoonVeil 2.1 (analysis)"
    order = 46
    timeout_key = "moonveil"
    families = ("MoonVeil", "MoonVeil 2.x")

    def __init__(self) -> None:
        self.dir = config.engine("moonveil")

    def available(self) -> tuple[bool, str]:
        if not os.path.isdir(os.path.join(self.dir, "moonveil21")):
            return False, "missing moonveil21 package"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        outdir = os.path.join(job_dir, "moonveil")
        os.makedirs(outdir, exist_ok=True)
        log = os.path.join(job_dir, "moonveil.log")
        cmd = [config.PYTHON, "-m", "moonveil21", "analyze", input_path,
               "-o", outdir, "--force"]
        code, to = await _run(cmd, self.dir, log, timeout)
        main = None
        for cand in ("reconstructed.luau", "decoded.partial.lua"):
            p = os.path.join(outdir, cand)
            if os.path.exists(p) and os.path.getsize(p) > 32:
                main = p
                break
        arts = [f for f in glob.glob(os.path.join(outdir, "*")) if os.path.isfile(f)]
        if main is None:
            return RunOutcome(False, self.name, note=self._tail(log))
        partial = True
        rep = os.path.join(outdir, "reconstruction.json")
        if os.path.exists(rep):
            import json
            try:
                partial = json.load(open(rep, encoding="utf-8")).get(
                    "status") != "full_verified"
            except Exception:
                pass
        note = "descrambled container; VM still present" if partial else ""
        return RunOutcome(True, self.name, main, arts, note, partial=partial,
                          extra={"log": log})


class IronVeil(Engine):
    """IronVeil v1 static deobfuscator (node)."""

    name = "ironveil"
    label = "IronVeil v1"
    order = 47
    timeout_key = "ironveil"
    families = ("IronVeil",)

    def __init__(self) -> None:
        self.dir = config.engine("ironveil")

    def available(self) -> tuple[bool, str]:
        if not shutil.which("node"):
            return False, "node not found"
        if not os.path.exists(os.path.join(self.dir, "index.js")):
            return False, "missing index.js"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "ironveil.log")
        cmd = ["node", "index.js", input_path, out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out], extra={"log": log})


class F77(Engine):
    """77fuscator -> stock Lua 5.1 chunk -> luadec source."""

    name = "f77"
    label = "77fuscator 0.4-0.6"
    order = 48
    timeout_key = "f77"
    families = ("77fuscator",)

    def __init__(self) -> None:
        self.dir = config.engine("f77")
        self.exe = os.path.join(self.dir, "deobfuscator.exe")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(self.exe):
            return False, "missing deobfuscator.exe"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        luac = os.path.join(job_dir, "deobf.luac")
        log = os.path.join(job_dir, "f77.log")
        code, to = await _run([self.exe, input_path, "-o", luac],
                              self.dir, log, timeout)
        if not (os.path.exists(luac) and os.path.getsize(luac) > 16):
            return RunOutcome(False, self.name, note=self._tail(log))
        out = os.path.join(job_dir, "deobf.lua")
        rust = config.tool("luadec")
        if os.path.exists(rust):
            await _run([rust, "--decompile", luac, out], job_dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        arts = [a for a in (luac, out) if os.path.exists(a)]
        if not ok:
            return RunOutcome(True, self.name, luac, arts,
                              "bytecode recovered; decompiler failed: "
                              + self._tail(log), partial=True,
                              extra={"log": log})
        return RunOutcome(True, self.name, out, arts, extra={"log": log})


class Wyn(Engine):
    """wYnFuscate: dynamic execution log in bundled Luau (first open tool)."""

    name = "wyn"
    label = "wYnFuscate"
    order = 49
    timeout_key = "wyn"
    can_trace = True
    families = ("wYnFuscate",)

    def __init__(self) -> None:
        self.dir = config.engine("wyn")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "dynamic", "wynfuscate_deob.py")):
            return False, "missing dynamic/wynfuscate_deob.py"
        if not os.path.exists(os.path.join(self.dir, "bin", "luau.exe")):
            return False, "missing bundled luau"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "wyn.log")
        cmd = [config.PYTHON, os.path.join("dynamic", "wynfuscate_deob.py"),
               input_path, "-o", out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        arts = [out]
        for f in glob.glob(os.path.join(os.path.dirname(input_path), "*strings*")):
            if os.path.isfile(f):
                arts.append(f)
        return RunOutcome(True, self.name, out, arts,
                          "behaviour log: reconstructed from a sandboxed run",
                          extra={"log": log})


class Ib3(Engine):
    """IronBrew3: static payload decode -> constants + opcode IR (partial)."""

    name = "ib3"
    label = "IronBrew 3 (static dump)"
    order = 26
    timeout_key = "ib3"
    families = ("IronBrew3 header",)

    def __init__(self) -> None:
        self.dir = config.engine("ib3")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "dump.py")):
            return False, "missing dump.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "ib3.log")
        cmd = [config.PYTHON, "dump.py", input_path, out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out],
                          "opcode IR + constants; semantics are per-build",
                          partial=True, extra={"log": log})


class Centurion(Engine):
    name = "centurion"
    label = "Centurion v6 / v6.1"
    order = 50
    timeout_key = "centurion"
    can_trace = True
    families = ("Centurion tag",)

    def __init__(self) -> None:
        self.dir = config.engine("centurion")
        self.tools = {
            "CENT_LUAU_LUAU": config.tool("luau"),
            "CENT_LUAU_LUAU_AST": config.tool("luau-ast"),
            "CENT_LUAU_LUAU_COMPILE": config.tool("luau-compile"),
            "CENT_SANDBOX": config.engine("luraph14", "deobf"),
        }

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "centurion.py")):
            return False, "missing centurion.py"
        for p in self.tools.values():
            if not os.path.exists(p):
                return False, f"missing {os.path.basename(p)}"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        head = _read(input_path, 262144)
        if "Centurion v" not in head and head.count("HA[") < 50:
            return RunOutcome(False, self.name, note="no centurion fingerprint")
        outdir = os.path.join(job_dir, "centurion")
        os.makedirs(outdir, exist_ok=True)
        log = os.path.join(job_dir, "centurion.log")
        cmd = [config.PYTHON, "centurion.py", "deobfuscate", input_path,
               "--out-dir", outdir]
        code, to = await _run(cmd, self.dir, log, timeout, env=_env(self.tools))
        files = [f for f in glob.glob(os.path.join(outdir, "*"))
                 if os.path.isfile(f)]
        lua = [f for f in files if f.endswith(".lua")
               and not f.endswith("_deobf_index.txt")]
        main = max(lua, key=os.path.getsize) if lua else None
        if not files:
            return RunOutcome(False, self.name, note=self._tail(log))
        text = _read(log)
        partial = "NEEDS" in text or "PARTIAL" in text
        note = "offline pass incomplete: live harness answers still missing" \
            if partial else ""
        return RunOutcome(main is not None, self.name, main, files, note,
                          partial=partial, extra={"log": log})


class Luarmor(Engine):
    name = "luarmor"
    label = "Luarmor loader"
    order = 60
    timeout_key = "luarmor"
    families = ("Luarmor mention", "Luarmor loader blob table",
                "Luarmor loader stub")

    def __init__(self) -> None:
        self.dir = config.engine("luarmor")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "main.py")):
            return False, "missing main.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        return await self.probe(job_dir, input_path, timeout)

    async def probe(self, job_dir: str, input_path: str, timeout: int) -> RunOutcome:
        outdir = os.path.join(job_dir, "luarmor")
        os.makedirs(outdir, exist_ok=True)
        log = os.path.join(job_dir, "luarmor.log")
        cmd = [config.PYTHON, "main.py", "probe", input_path, "--json",
               "--split", outdir]
        code, to = await _run(cmd, self.dir, log, timeout)
        files = [f for f in glob.glob(os.path.join(outdir, "*")) if os.path.isfile(f)]
        main = None
        for cand in ("payload.lua", "loader.lua"):
            p = os.path.join(outdir, cand)
            if os.path.exists(p):
                main = p
                break
        if files:
            return RunOutcome(bool(files), self.name, main, files,
                              "" if files else self._tail(log),
                              extra={"log": log})
        text = _read(log)
        start = text.find("{")
        report = None
        if start >= 0:
            try:
                import json
                report = json.loads(text[start:text.rfind("}") + 1])
            except Exception:
                report = None
        if report and (report.get("signature_hits") or report.get("luarmor_client")):
            probe = os.path.join(job_dir, "luarmor_probe.json")
            import json
            with open(probe, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            note = "loader stub classified; the payload needs a live fetch"
            return RunOutcome(True, self.name, probe, [probe], note,
                              partial=True, extra={"log": log})
        return RunOutcome(False, self.name, note=self._tail(log))

    async def fetch(self, job_dir: str, url: str, timeout: int) -> RunOutcome:
        outdir = os.path.join(job_dir, "luarmor")
        os.makedirs(outdir, exist_ok=True)
        log = os.path.join(job_dir, "luarmor.log")
        cmd = [config.PYTHON, "main.py", "fetch", "--loader-url", url,
               "--repo", config.engine("luraph15"), "--output", outdir]
        code, to = await _run(cmd, self.dir, log, timeout)
        files = [f for f in glob.glob(os.path.join(outdir, "**", "*"), recursive=True)
                 if os.path.isfile(f)]
        lua = [f for f in files if f.endswith((".lua", ".luau"))]
        main = max(lua, key=os.path.getsize) if lua else None
        return RunOutcome(bool(files), self.name, main, files,
                          "" if files else self._tail(log), extra={"log": log})


class Strings(Engine):
    """Last-resort static recovery: literals, URLs, decoded payloads."""

    name = "strings"
    label = "static strings (family-agnostic)"
    order = 95
    timeout_key = "strings"
    families = ()

    def __init__(self) -> None:
        self.dir = config.engine("strings")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "scan.py")):
            return False, "missing scan.py"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        out = os.path.join(job_dir, "deobf.lua")
        log = os.path.join(job_dir, "strings.log")
        cmd = [config.PYTHON, "scan.py", input_path, out]
        code, to = await _run(cmd, self.dir, log, timeout)
        ok = os.path.exists(out) and os.path.getsize(out) > 32
        if not ok:
            return RunOutcome(False, self.name, note=self._tail(log))
        return RunOutcome(True, self.name, out, [out],
                          "no engine matched: this is the data the script "
                          "carries (strings/URLs/payloads), not source",
                          partial=True, extra={"log": log})


class Unveilr(Engine):
    """Universal executor-env tracer (Lune + Luau AST re-emitter).

    Runs the script inside a mocked executor environment and renders the
    recorded effects back to Luau. Last resort for families without a
    dedicated engine.
    """

    name = "unveilr"
    label = "unveilr (universal trace)"
    order = 90
    timeout_key = "unveilr"
    can_trace = True
    families = ()

    def __init__(self) -> None:
        self.dir = config.engine("unveilr")

    def available(self) -> tuple[bool, str]:
        if not os.path.exists(os.path.join(self.dir, "lune.exe")):
            return False, "missing lune.exe"
        if not os.path.exists(os.path.join(self.dir, "main.luau")):
            return False, "missing main.luau"
        return True, ""

    async def run(self, job_dir, input_path, trace, timeout) -> RunOutcome:
        if os.path.getsize(input_path) > 1024 * 1024:
            return RunOutcome(False, self.name,
                              note="input over 1 MB: tracer cannot finish")
        tmp = os.path.join(self.dir, "bot_tmp")
        os.makedirs(tmp, exist_ok=True)
        name = f"job_{os.getpid()}_{int(time.time() * 1000) % 100000}.lua"
        src = os.path.join(tmp, name)
        produced = src + ".out"
        shutil.copyfile(input_path, src)
        log = os.path.join(job_dir, "unveilr.log")
        env = _env({"HOOKOP_BIN": os.path.join(self.dir, "lute.exe")})
        try:
            cmd = [os.path.join(self.dir, "lune.exe"), "run", "main.luau",
                   f"bot_tmp/{name}", f"out=bot_tmp/{name}.out"]
            await _run(cmd, self.dir, log, timeout, env=env)
            if not os.path.exists(produced) or os.path.getsize(produced) < 16:
                return RunOutcome(False, self.name, note=self._tail(log))
            body = _read(produced)
            if body.startswith("--err"):
                return RunOutcome(False, self.name,
                                  note=body[5:].strip()[:300] or self._tail(log))
            out = os.path.join(job_dir, "deobf.lua")
            with open(out, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            return RunOutcome(True, self.name, out, [out],
                              "behaviour reconstruction: effects logged from a "
                              "sandboxed run", partial=True,
                              extra={"log": log})
        finally:
            for p in (src, produced):
                try:
                    os.remove(p)
                except OSError:
                    pass


ENGINES: dict[str, Engine] = {
    e.name: e for e in (Bytecode(), Luraph15(), Ib1(), Luraph14(), Ib2Net(),
                        Ironbrew2(), Ib3(), Prometheus(), LuaObfuscator(),
                        Moonsec(), Orrxl4(), SynapseXen(), Psu(), Hercules(),
                        MoonVeil(), IronVeil(), F77(), Wyn(), Centurion(),
                        Luarmor(), Unveilr(), Strings())
}
ORDER = sorted(ENGINES.values(), key=lambda e: e.order)

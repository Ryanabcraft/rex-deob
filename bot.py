"""Discord front end. Commands: .l, .get, .detect, .trace, .engines."""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import time
import zipfile

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core import analyzer, config, detector, downloader, pipeline
from core.engines import ENGINES, ORDER

load_dotenv(os.path.join(config.ROOT, ".env"))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=config.PREFIX, intents=intents,
                   help_command=None)

SEM = asyncio.Semaphore(config.MAX_PARALLEL)
_running: set[int] = set()
_cooldown: dict[int, float] = {}

COLORS = {"CLEAN": 0x2ECC71, "LOW": 0x95A5A6, "MED": 0xF1C40F, "HIGH": 0xE74C3C}

FENCE = re.compile(r"```(?:lua|luau)?\s*\n(.*?)```", re.S)


def _bar(pct: int, width: int = 12) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "▰" * filled + "▱" * (width - filled)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"


def _live_embed(state: dict, source: str) -> discord.Embed:
    elapsed = time.time() - state["t0"]
    pct = state["pct"]
    shown = pct if pct >= 100 else min(95, pct + min(12, int(elapsed / 25)))
    embed = discord.Embed(title="Deobfuscating",
                          color=0x5865F2 if shown < 100 else 0x2ECC71)
    embed.add_field(name="File", value=f"`{source}`"[:1020], inline=False)
    embed.add_field(name="Stage", value=state["stage"][:1020], inline=False)
    embed.add_field(name="Progress", value=f"`{_bar(shown)}`  **{shown}%**",
                    inline=True)
    embed.add_field(name="Elapsed", value=_fmt_elapsed(elapsed), inline=True)
    embed.set_footer(text="REX DEOBF · long scripts (Luraph v15.1) take minutes "
                          "by design")
    return embed


async def _tick(status: discord.Message, state: dict, source: str) -> None:
    while True:
        try:
            await asyncio.sleep(4)
            await status.edit(embed=_live_embed(state, source))
        except asyncio.CancelledError:
            return
        except discord.HTTPException:
            pass


def _stem(name: str) -> str:
    base = os.path.splitext(os.path.basename(name or "script"))[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "script"


async def _read_target(ctx: commands.Context) -> tuple[bytes, str, str | None]:
    """(data, filename, url) from attachment, replied message, link or code block."""
    target = ctx.message
    if ctx.message.reference and isinstance(ctx.message.reference.resolved,
                                           discord.Message):
        target = ctx.message.reference.resolved

    if target.attachments:
        att = target.attachments[0]
        if att.size > config.MAX_FILE_BYTES:
            raise ValueError(f"file larger than {config.MAX_FILE_MB}MB")
        if not att.filename.lower().endswith(config.ALLOWED_EXT):
            raise ValueError("send .lua, .luau, .txt or .zip")
        return await att.read(), downloader.safe_name(att.filename), None

    url = downloader.find_url(ctx.message.content or "")
    if not url and target is not ctx.message:
        url = downloader.find_url(target.content or "")
    if url:
        data, name = await downloader.fetch(url)
        return data, name, url

    m = FENCE.search(ctx.message.content or "")
    if m and len(m.group(1)) > 60:
        return m.group(1).encode("utf-8"), "pasted.lua", None

    raise ValueError("attach a script, drop a raw link, or reply to one with "
                     f"`{config.PREFIX}l`")


def _summary_embed(res: pipeline.JobResult, source: str) -> discord.Embed:
    risk = res.analysis.risk if res.analysis else "CLEAN"
    embed = discord.Embed(title="Partial lift" if res.partial
                          else "Deobfuscation finished",
                          color=COLORS.get(risk, 0x5865F2))
    embed.add_field(name="Source", value=f"`{source}`"[:1020], inline=False)
    det = res.detection
    embed.add_field(name="Engine",
                    value=f"`{res.engine or 'none'}`"
                          + (f"  (auto: `{det.family}` {det.confidence:.2f})"
                             if det.family else ""),
                    inline=True)
    embed.add_field(name="Risk", value=f"**{risk}**", inline=True)
    embed.add_field(name="Time", value=f"{res.seconds:.0f}s", inline=True)
    if res.analysis:
        top = [f"`{f.severity}` {f.label} x{f.count}"
               for f in res.analysis.findings[:6]]
        embed.add_field(name="Indicators",
                        value="\n".join(top) or "none matched", inline=False)
        embed.add_field(name="Output",
                        value=f"{res.analysis.lines} lines / "
                              f"{res.analysis.bytes} bytes", inline=True)
    q = res.quality
    if q:
        flags = ["parses" if q.parses else "does not parse"]
        if q.placeholders:
            flags.append(f"{q.placeholders} placeholder(s)")
        if q.report:
            flags.append("report only")
        embed.add_field(name="Quality",
                        value=f"`{q.score:.2f}` · " + " · ".join(flags),
                        inline=True)
    if res.partial:
        embed.add_field(name="Partial", inline=False,
                        value="no engine reached a complete lift; this is the "
                              "best result the chain produced")
    if res.note and not res.ok:
        embed.add_field(name="Note", value=res.note[:1000], inline=False)
    embed.set_footer(text="deobf bot · " + (res.engine or "no engine"))
    return embed


def _pack_files(res: pipeline.JobResult, source_name: str) -> list[discord.File]:
    stem = _stem(source_name)
    out: list[discord.File] = []
    seen: set[str] = set()
    order = [res.main] if res.main else []
    order += [a for a in res.artifacts if a != res.main]
    for path in order:
        if not path or path in seen or not os.path.isfile(path):
            continue
        seen.add(path)
        size = os.path.getsize(path)
        if size > 24 * 1024 * 1024:
            continue
        ext = os.path.splitext(path)[1].lower()
        base = os.path.basename(path)
        if ext in (".lua", ".luau"):
            name = f"{stem}.deobf{ext}" if path == res.main else f"{stem}.{base}"
        elif ext == ".luac":
            name = f"{stem}.deobf.luac"
        elif base in ("analysis.txt", "strings.txt", "report.json"):
            name = f"{stem}.{base}"
        else:
            name = f"{stem}.{base}"
        out.append(discord.File(path, filename=name))
        if len(out) >= 6:
            break
    if len(out) > 1:
        total = sum(os.path.getsize(p) for p in order[:6] if os.path.isfile(p))
        if total > 20 * 1024 * 1024:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in out:
                    with open(f.fp.name, "rb") as src:
                        zf.writestr(f.filename, src.read())
            buf.seek(0)
            return [discord.File(buf, filename=f"{stem}.deobf.zip")]
    return out


async def _run_and_send(ctx: commands.Context, trace: bool, force: str | None,
                        only_detect: bool = False) -> None:
    uid = ctx.author.id
    if uid in _running:
        await ctx.reply("your job is still running.")
        return
    now = time.time()
    wait = config.COOLDOWN_S - (now - _cooldown.get(uid, 0.0))
    if wait > 0:
        await ctx.reply(f"cooldown: {wait:.0f}s.")
        return

    try:
        data, name, url = await _read_target(ctx)
    except Exception as exc:
        await ctx.reply(f"{exc}")
        return

    _running.add(uid)
    _cooldown[uid] = now
    status = await ctx.reply("queued…")
    job_dir = pipeline.new_workdir()
    files: list[discord.File] = []
    state = {"pct": 0, "stage": "queued", "t0": time.time(), "engine": None}
    ticker: asyncio.Task | None = None
    try:
        if url and "luarmor.net" in url:
            eng = ENGINES["luarmor"]
            ok, why = eng.available()
            if not ok:
                await status.edit(content=f"luarmor engine unavailable: {why}")
                return
            await status.edit(content="luarmor loader: fetching…")
            got = await eng.fetch(job_dir, url, config.TIMEOUTS["luarmor"])
            if not got.main:
                await status.edit(content="luarmor fetch produced nothing; "
                                          "run `.l` with the loader file instead")
                return
            path, name = got.main, os.path.basename(got.main)
        else:
            path, name = downloader.prepare_script(data, name, job_dir)

        if only_detect:
            text = pipeline.decode(open(path, "rb").read())
            det = detector.detect(text)
            embed = discord.Embed(title="Fingerprint", color=COLORS["LOW"])
            embed.add_field(name="Engine",
                            value=f"`{det.engine or 'unknown'}` "
                                  f"{det.confidence:.2f}", inline=True)
            embed.add_field(name="Lua", value=str(det.lua_like), inline=True)
            embed.add_field(name="Evidence",
                            value="\n".join(det.evidence[:6]) or "none",
                            inline=False)
            await status.edit(content=None, embed=embed)
            return

        async with SEM:
            async def progress(pct: int, msg: str) -> None:
                state["pct"] = pct
                state["stage"] = msg
                if "·" in msg:
                    state["engine"] = msg.split("·")[0].strip()
                try:
                    await status.edit(embed=_live_embed(state, name))
                except discord.HTTPException:
                    pass

            ticker = asyncio.create_task(_tick(status, state, name))
            res = await asyncio.wait_for(
                pipeline.run_job(path, trace=trace, force_engine=force,
                                 progress=progress),
                timeout=config.JOB_TIMEOUT_S)
            ticker.cancel()

        if not res.ok:
            note = res.note[:1500] or "no engine produced output"
            await status.edit(content=f"failed: {note}\nworkdir: `{job_dir}`",
                              embed=None)
            return

        files = _pack_files(res, name)
        embed = _summary_embed(res, name + (f" · {url}" if url else ""))
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        await ctx.reply(embed=embed, files=files or None)
    except asyncio.TimeoutError:
        await status.edit(content=f"job timeout ({config.JOB_TIMEOUT_S}s).")
    except Exception as exc:
        await status.edit(content=f"error: {exc}")
        raise
    finally:
        if ticker and not ticker.done():
            ticker.cancel()
        _running.discard(uid)
        for f in files:
            try:
                f.close()
            except Exception:
                pass
        if not config.KEEP_JOBS:
            shutil.rmtree(job_dir, ignore_errors=True)


@bot.command(name="l")
async def l_cmd(ctx: commands.Context, *, arg: str | None = None) -> None:
    """Auto-detect and deobfuscate (attachment, reply, link or pasted block)."""
    await _run_and_send(ctx, trace=False, force=None)


@bot.command(name="get")
async def get_cmd(ctx: commands.Context, *, arg: str | None = None) -> None:
    """Download a script from a link and deobfuscate it."""
    url = downloader.find_url(arg or ctx.message.content or "")
    if not url:
        await ctx.reply(f"usage: `{config.PREFIX}get <url>`")
        return
    await _run_and_send(ctx, trace=False, force=None)


@bot.command(name="trace")
async def trace_cmd(ctx: commands.Context, *, arg: str | None = None) -> None:
    """Behaviour trace instead of full devirtualization (where supported)."""
    await _run_and_send(ctx, trace=True, force=None)


@bot.command(name="detect")
async def detect_cmd(ctx: commands.Context, *, arg: str | None = None) -> None:
    """Fingerprint the obfuscator without running an engine."""
    await _run_and_send(ctx, trace=False, force=None, only_detect=True)


@bot.command(name="engines")
async def engines_cmd(ctx: commands.Context) -> None:
    lines = []
    for eng in ORDER:
        ok, why = eng.available()
        lines.append(f"{'✅' if ok else '❌'} `{eng.name}` — {eng.label}"
                     + ("" if ok else f" ({why})"))
    embed = discord.Embed(title="Engines", description="\n".join(lines),
                          color=0x5865F2)
    await ctx.reply(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    p = config.PREFIX
    text = (
        f"**{p}l** — attach a script, reply to one, drop a link or paste a "
        f"code block. Auto-detects the obfuscator and deobfuscates.\n"
        f"**{p}get <url>** — download from a link and deobfuscate.\n"
        f"**{p}trace** — behaviour trace only (Luraph 14/15, MoonSec).\n"
        f"**{p}detect** — fingerprint only, no engine run.\n"
        f"**{p}engines** — engine list and availability.\n\n"
        f"Engines: Luraph v15/v15.1 · Luraph v14/IronBrew · "
        f"Prometheus/WeAreDevs · MoonSec V3 · Centurion · Luarmor."
    )
    await ctx.reply(text)


@bot.event
async def on_ready() -> None:
    print(f"deobf bot online as {bot.user} (prefix {config.PREFIX!r})")
    for eng in ORDER:
        ok, why = eng.available()
        print(f"  engine {eng.name:<10} {'ready' if ok else 'MISSING: ' + why}")


def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN missing: copy .env.example to .env")
    bot.run(config.TOKEN)


if __name__ == "__main__":
    main()

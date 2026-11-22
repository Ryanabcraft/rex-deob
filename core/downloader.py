"""URL fetch for .get / .l <link>, with SSRF guard and a hard size cap."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import os
import re
import socket
import zipfile
from urllib.parse import urlparse

import aiohttp

from . import config

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.I)


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,);:`'\"")


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


async def _validate(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("protocol not allowed")
    if not parsed.hostname:
        raise ValueError("invalid URL")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = await asyncio.get_running_loop().run_in_executor(
        None, lambda: socket.getaddrinfo(parsed.hostname, port,
                                         type=socket.SOCK_STREAM))
    ips = {i[4][0] for i in infos}
    if not ips:
        raise ValueError("host has no IP")
    for ip in ips:
        if not _is_public_ip(ip):
            raise ValueError("access to private IP blocked")


def safe_name(name: str | None, default: str = "script.lua") -> str:
    base = os.path.basename(name or default)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or default


async def fetch(url: str, max_bytes: int | None = None,
                timeout: int = 45) -> tuple[bytes, str]:
    await _validate(url)
    cap = max_bytes or config.MAX_FILE_BYTES
    jar = aiohttp.CookieJar(unsafe=False)
    headers = {"User-Agent": "Mozilla/5.0 (deobf-bot)"}
    async with aiohttp.ClientSession(cookie_jar=jar) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout),
                               headers=headers, allow_redirects=True,
                               max_redirects=5) as resp:
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                buf.extend(chunk)
                if len(buf) > cap:
                    raise ValueError(f"file larger than {cap // (1024 * 1024)}MB")
            name = safe_name(os.path.basename(urlparse(str(resp.url)).path)
                             or "script.lua")
            return bytes(buf), name


def prepare_script(data: bytes, name: str, workdir: str) -> tuple[str, str]:
    """Write bytes to disk. Zips get their biggest Lua member extracted."""
    path = os.path.join(workdir, safe_name(name))
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.infolist()
                       if not m.is_dir()
                       and m.filename.lower().endswith(config.LUA_EXT)]
            if not members:
                raise ValueError("zip has no .lua/.luau/.txt inside")
            best = max(members, key=lambda m: m.file_size)
            data = zf.read(best)
            path = os.path.join(workdir, safe_name(best.filename, "script.lua"))
    with open(path, "wb") as f:
        f.write(data)
    return path, os.path.basename(path)

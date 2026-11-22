import gzip
import os
import random
import re
import time
import urllib.error
import urllib.request
import zlib

from .layers import HEADER, SKIP

RAW_ENDPOINT = "https://orrxl4-protector.com/api/raw?id="
ID_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")
HTML_MARKERS = (b"<!doctype", b"<html", b"<head", b"<body", b"<script", b"<meta", b"<title")

PROFILES = [
    {"User-Agent": "Roblox/WinInet"},
    {"User-Agent": "RobloxStudio/WinInet"},
    {"User-Agent": "Roblox/WinInet", "Accept": "*/*", "Connection": "close"},
    {"User-Agent": "Roblox/Linux"},
    {"User-Agent": "Roblox/Darwin"},
    {"User-Agent": "Synapse-X"},
    {"User-Agent": "Delta/1.0"},
    {"User-Agent": "Fluxus"},
    {"User-Agent": "KRNL"},
    {"User-Agent": "Wave/1.0"},
    {"User-Agent": "Solara"},
    {"User-Agent": "curl/8.5.0", "Accept": "*/*"},
    {"User-Agent": "python-requests/2.31.0", "Accept": "*/*"},
    {"User-Agent": "Wget/1.21.4", "Accept": "*/*"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
]


class FetchError(Exception):
    pass


def normalize(body):
    return body.lstrip(SKIP)


def is_html(body):
    head = normalize(body)[:2048].lower()
    if head.startswith(HEADER.lower()):
        return False
    return any(m in head for m in HTML_MARKERS)


def has_protection_header(body):
    return normalize(body).startswith(HEADER)


def accept(body, require_header):
    if not body.strip():
        return False
    if require_header:
        return has_protection_header(body)
    return not is_html(body)


def inflate(body, encoding):
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        return body
    return body


def to_url(target):
    if re.match(r"^https?://", target, re.I):
        return target
    if ID_RE.match(target):
        return RAW_ENDPOINT + target
    return None


class Fetcher:
    def __init__(self, retries=45, delay=0.7, jitter=0.5, timeout=25.0, user_agent=None, headers=None, log=None):
        self.retries = retries
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        profiles = [dict(p) for p in PROFILES]
        if user_agent or headers:
            custom = {}
            if user_agent:
                custom["User-Agent"] = user_agent
            custom.update(headers or {})
            profiles.insert(0, custom)
        self.profiles = profiles

    def request(self, url, profile):
        headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        headers.update(profile)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                return resp.status, resp.headers.get("Content-Type", ""), inflate(body, resp.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            return e.code, e.headers.get("Content-Type", "") if e.headers else "", inflate(body, e.headers.get("Content-Encoding") if e.headers else None)

    def fetch(self, url, require_header=True):
        last = "no attempt made"
        for attempt in range(self.retries):
            profile = self.profiles[attempt % len(self.profiles)]
            ua = profile.get("User-Agent", "?")
            try:
                status, ctype, body = self.request(url, profile)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last = "attempt %d (%s): %s" % (attempt + 1, ua, e)
                self.log(last)
            else:
                if accept(body, require_header):
                    self.log("attempt %d (%s): got %d bytes" % (attempt + 1, ua, len(body)))
                    return normalize(body) if require_header else body
                kind = "html" if is_html(body) else "unexpected content"
                text = body.decode("utf-8", "replace")
                m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                snippet = (m.group(1) if m else re.sub(r"<[^>]+>", " ", text)).strip()
                snippet = " ".join(snippet.split())[:160]
                last = "attempt %d (%s): HTTP %s, %s, %s (%d bytes) [%s] url=%s" % (attempt + 1, ua, status, ctype or "?", kind, len(body), snippet, url)
                self.log(last)
            if attempt + 1 < self.retries:
                time.sleep(self.delay + random.random() * self.jitter)
        raise FetchError("gave up after %d attempts; last: %s" % (self.retries, last))


def load_source(target, fetcher):
    if os.path.isfile(target):
        with open(target, "rb") as fh:
            return fh.read(), "file"
    url = to_url(target)
    if url is None:
        raise FetchError("%r is not a file, URL or protector id" % target)
    return fetcher.fetch(url, require_header=True), url

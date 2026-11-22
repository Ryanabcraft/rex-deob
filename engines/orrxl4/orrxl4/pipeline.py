import re

from .layers import peel, has_header, DeobfError
from .reconstruct import reconstruct
from .fetch import Fetcher, load_source, to_url

URL_RE = re.compile(rb"""https?://[^\s"'\)\]\}]+""")
HTTPGET_RE = re.compile(rb"""HttpGet(?:Async)?\s*\(\s*(?:"([^"]+)"|'([^']+)')""")


class Result:
    def __init__(self, origin, raw, final, clean, layers, urls, remote):
        self.origin = origin
        self.raw = raw
        self.final = final
        self.clean = clean
        self.layers = layers
        self.urls = urls
        self.remote = remote


def extract_urls(code):
    seen = []
    for m in URL_RE.finditer(code):
        u = m.group(0).decode("latin-1")
        if u not in seen:
            seen.append(u)
    return seen


def remote_targets(code):
    out = []
    for m in HTTPGET_RE.finditer(code):
        u = (m.group(1) or m.group(2)).decode("latin-1")
        if u not in out:
            out.append(u)
    return out


def deobfuscate_bytes(data, origin="input", rename=True, timeout=300.0, max_layers=64, log=None, fast=True):
    final, layers = peel(data, max_layers=max_layers, timeout=timeout, log=log, fast=fast)
    clean = reconstruct(final, rename=rename)
    return Result(origin, data, final, clean, layers, extract_urls(final), remote_targets(final))


def deobfuscate(target, fetcher=None, rename=True, timeout=300.0, max_layers=64, follow=False, max_follow=4, log=None, fast=True):
    fetcher = fetcher or Fetcher(log=log)
    data, origin = load_source(target, fetcher)
    results = [deobfuscate_bytes(data, origin, rename, timeout, max_layers, log, fast)]
    seen = {origin}
    while follow and len(results) <= max_follow:
        nxt = None
        for u in results[-1].remote:
            if u not in seen and to_url(u):
                nxt = u
                break
        if nxt is None:
            break
        seen.add(nxt)
        if log:
            log("following %s" % nxt)
        body = fetcher.fetch(nxt, require_header=False)
        if not has_header(body):
            results.append(Result(nxt, body, body, reconstruct(body, rename=rename), [], extract_urls(body), remote_targets(body)))
            break
        results.append(deobfuscate_bytes(body, nxt, rename, timeout, max_layers, log, fast))
    return results

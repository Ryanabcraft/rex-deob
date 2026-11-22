import time

from . import sandbox

HEADER = b"-- PROTECTED BY ORRXL4 OBFUSCATOR"
SKIP = b"\xef\xbb\xbf \t\r\n"


class DeobfError(Exception):
    pass


class Layer:
    def __init__(self, index, size_in, size_out, captures, seconds):
        self.index = index
        self.size_in = size_in
        self.size_out = size_out
        self.captures = captures
        self.seconds = seconds


def has_header(code):
    return code.lstrip(SKIP).startswith(HEADER)


def looks_like_loader(code):
    if has_header(code):
        return True
    return b"loadstring or load" in code[:6000] and b"bxor" in code


def peel(code, max_layers=64, timeout=300.0, log=None, fast=True):
    layers = []
    current = code
    while looks_like_loader(current):
        index = len(layers) + 1
        if index > max_layers:
            raise DeobfError("exceeded %d layers" % max_layers)
        started = time.monotonic()
        result = sandbox.run(current, timeout=timeout, fast=fast)
        if not result.captures:
            reason = result.error or "the loader never called loadstring/load"
            raise DeobfError("layer %d produced no code: %s" % (index, reason))
        nxt = result.captures[-1]
        layer = Layer(index, len(current), len(nxt), len(result.captures), time.monotonic() - started)
        layers.append(layer)
        if log:
            log("layer %d: %d -> %d bytes (%.2fs)" % (index, layer.size_in, layer.size_out, layer.seconds))
        if nxt == current:
            raise DeobfError("layer %d decoded to itself" % index)
        current = nxt
    return current, layers

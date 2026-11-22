import sys
import time

from .lua.lexer import LexError
from .lua.parser import parse, ParseError
from .lua.interp import Compiler, Limits
from .lua.runtime import LuaError, SandboxLimit
from .lua.stdlib import build_globals, Hook, ERRORS
from .fastpaths import FastPaths


class SandboxResult:
    def __init__(self):
        self.captures = []
        self.prints = []
        self.error = None
        self.elapsed = 0.0


def run(code, timeout=300.0, fast=True):
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 12000))
    result = SandboxResult()
    hook = Hook()
    started = time.monotonic()
    try:
        text = code.decode("latin-1") if isinstance(code, (bytes, bytearray)) else code
        tree = parse(text)
        genv = build_globals(hook)
        limits = Limits(timeout)
        main = Compiler(genv, limits, FastPaths(genv) if fast else None).compile_chunk(tree)
        main()
    except LuaError as e:
        v = e.value
        result.error = v.decode("latin-1") if isinstance(v, bytes) else repr(v)
    except SandboxLimit as e:
        result.error = str(e)
    except (LexError, ParseError) as e:
        result.error = "syntax error: %s" % e
    except ERRORS as e:
        result.error = "%s: %s" % (type(e).__name__, e)
    result.captures = hook.captures
    result.prints = hook.prints
    result.elapsed = time.monotonic() - started
    return result

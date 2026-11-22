import functools
import math
import random
import re

from . import runtime as R
from .runtime import LuaTable, LuaError, SandboxLimit
from .interp import NEXT

ERRORS = (LuaError, ArithmeticError, TypeError, ValueError, IndexError, KeyError, RecursionError, AttributeError)


def err(msg):
    return LuaError(msg.encode() if isinstance(msg, str) else msg)


def check_str(v, n=1, fn="?"):
    c = v.__class__
    if c is bytes:
        return v
    if c is int or c is float:
        return R.tostr(v)
    raise err("bad argument #%d to '%s' (string expected, got %s)" % (n, fn, R.type_name(v) if v is not None else "no value"))


def check_num(v, n=1, fn="?"):
    x = R.to_number(v)
    if x is None:
        raise err("bad argument #%d to '%s' (number expected, got %s)" % (n, fn, R.type_name(v) if v is not None else "no value"))
    return x


def check_int(v, n=1, fn="?"):
    x = check_num(v, n, fn)
    if x.__class__ is float:
        if x != x or x in (math.inf, -math.inf):
            raise err("bad argument #%d to '%s' (number has no integer representation)" % (n, fn))
        return int(x)
    return x


def opt_int(v, default, n=1, fn="?"):
    return default if v is None else check_int(v, n, fn)


def check_table(v, n=1, fn="?"):
    if v.__class__ is not LuaTable:
        raise err("bad argument #%d to '%s' (table expected, got %s)" % (n, fn, R.type_name(v) if v is not None else "no value"))
    return v


def table_from_list(values):
    t = LuaTable()
    for i, v in enumerate(values):
        t.set(i + 1, v)
    return t


def reg(table, name, fn):
    table.set(name.encode(), fn)


def lib(**funcs):
    t = LuaTable()
    for k, v in funcs.items():
        t.set(k.encode(), v)
    return t


LUA_CLASSES = {
    "a": "A-Za-z", "d": "0-9", "l": "a-z", "s": " \\t\\n\\r\\f\\v", "u": "A-Z",
    "w": "A-Za-z0-9", "x": "0-9A-Fa-f", "p": "!-/:-@\\[-`{-~", "c": "\\x00-\\x1f\\x7f", "g": "!-~",
}


def class_body(ch):
    return LUA_CLASSES[ch.lower()]


def pattern_to_regex(pat):
    p = pat.decode("latin-1")
    i = 0
    n = len(p)
    out = []
    anchored = False
    if p.startswith("^"):
        anchored = True
        i = 1
    pos_captures = []
    group = 0
    while i < n:
        c = p[i]
        if c == "%":
            i += 1
            if i >= n:
                raise err("malformed pattern (ends with '%')")
            d = p[i]
            if d in "bf":
                raise err("pattern item %%%s is not supported" % d)
            if d.isdigit():
                out.append("(?:\\%s)" % d)
            elif d.lower() in LUA_CLASSES and d.isalpha():
                out.append(("[^%s]" if d.isupper() else "[%s]") % class_body(d))
            else:
                out.append(re.escape(d))
            i += 1
        elif c == "[":
            j = i + 1
            neg = False
            if j < n and p[j] == "^":
                neg = True
                j += 1
            body = []
            first = True
            while j < n and (p[j] != "]" or first):
                first = False
                if p[j] == "%" and j + 1 < n:
                    d = p[j + 1]
                    if d.isalpha() and d.lower() in LUA_CLASSES:
                        if d.isupper():
                            raise err("negated class inside set is not supported")
                        body.append(class_body(d))
                    else:
                        body.append(re.escape(d))
                    j += 2
                elif p[j] == "-" and body and j + 1 < n and p[j + 1] != "]":
                    body.append("-")
                    j += 1
                else:
                    body.append(re.escape(p[j]) if p[j] in "\\]^[" else p[j])
                    j += 1
            if j >= n:
                raise err("malformed pattern (missing ']')")
            out.append("[%s%s]" % ("^" if neg else "", "".join(body)))
            i = j + 1
        elif c == ".":
            out.append(".")
            i += 1
        elif c in "*+?":
            out.append(c)
            i += 1
        elif c == "-":
            out.append("*?")
            i += 1
        elif c == "(":
            group += 1
            if i + 1 < n and p[i + 1] == ")":
                pos_captures.append(group)
                out.append("()")
                i += 2
            else:
                out.append("(")
                i += 1
        elif c == ")":
            out.append(")")
            i += 1
        elif c == "$" and i == n - 1:
            out.append("\\Z")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    rx = re.compile("".join(out).encode("latin-1"), re.DOTALL)
    return rx, anchored, pos_captures


def captures_of(m, pos_captures, whole_if_none=True):
    if m.re.groups == 0:
        return [m.group(0)] if whole_if_none else []
    out = []
    for gi in range(1, m.re.groups + 1):
        if gi in pos_captures:
            out.append(m.start(gi) + 1)
        else:
            out.append(m.group(gi))
    return out


def find_match(rx, anchored, s, init):
    if anchored:
        m = rx.match(s, init)
        return m
    return rx.search(s, init)


def build_string_lib(genv, hook):
    def s_len(s=None, *_):
        return [len(check_str(s, 1, "len"))]

    def s_sub(s=None, i=None, j=None, *_):
        s = check_str(s, 1, "sub")
        n = len(s)
        i = opt_int(i, 1, 2, "sub")
        j = opt_int(j, -1, 3, "sub")
        if i < 0:
            i = max(n + i + 1, 1)
        elif i == 0:
            i = 1
        if j < 0:
            j = n + j + 1
        elif j > n:
            j = n
        return [s[i - 1:j] if i <= j else b""]

    def s_byte(s=None, i=None, j=None, *_):
        s = check_str(s, 1, "byte")
        n = len(s)
        i = opt_int(i, 1, 2, "byte")
        j = opt_int(j, i, 3, "byte")
        if i < 0:
            i = n + i + 1
        if j < 0:
            j = n + j + 1
        i = max(i, 1)
        j = min(j, n)
        return [s[k - 1] for k in range(i, j + 1)]

    def s_char(*args):
        out = bytearray()
        for k, a in enumerate(args):
            v = check_int(a, k + 1, "char")
            if not 0 <= v <= 255:
                raise err("bad argument #%d to 'char' (invalid value)" % (k + 1))
            out.append(v)
        return [bytes(out)]

    def s_upper(s=None, *_):
        return [check_str(s, 1, "upper").upper()]

    def s_lower(s=None, *_):
        return [check_str(s, 1, "lower").lower()]

    def s_rep(s=None, n=None, sep=None, *_):
        s = check_str(s, 1, "rep")
        n = check_int(n, 2, "rep")
        if n <= 0:
            return [b""]
        if len(s) * n > 64 * 1024 * 1024:
            raise SandboxLimit("string.rep result too large")
        if sep is None:
            return [s * n]
        return [check_str(sep, 3, "rep").join([s] * n)]

    def s_reverse(s=None, *_):
        return [check_str(s, 1, "reverse")[::-1]]

    def s_format(fmt=None, *args):
        f = check_str(fmt, 1, "format").decode("latin-1")
        spec_re = re.compile(r"%([-+ #0]*)(\d*)(?:\.(\d*))?([diouxXeEfgGcsqa%])")
        out = []
        last = 0
        ai = 0
        for m in spec_re.finditer(f):
            out.append(f[last:m.start()])
            last = m.end()
            flags, width, prec, conv = m.groups()
            if conv == "%":
                out.append("%")
                continue
            if ai >= len(args):
                raise err("bad argument #%d to 'format' (no value)" % (ai + 2))
            a = args[ai]
            ai += 1
            spec = "%" + flags + width + ("." + prec if prec is not None else "")
            if conv in "di":
                out.append((spec + "d") % check_int(a, ai + 1, "format"))
            elif conv == "u":
                out.append((spec + "d") % (check_int(a, ai + 1, "format") & 0xFFFFFFFFFFFFFFFF))
            elif conv in "oxX":
                v = check_int(a, ai + 1, "format")
                out.append((spec + conv) % (v if v >= 0 else v & 0xFFFFFFFFFFFFFFFF))
            elif conv in "eEfgG":
                out.append((spec + conv) % float(check_num(a, ai + 1, "format")))
            elif conv == "a":
                out.append(float(check_num(a, ai + 1, "format")).hex())
            elif conv == "c":
                out.append(chr(check_int(a, ai + 1, "format")))
            elif conv == "s":
                out.append((spec + "s") % R.tostr(a).decode("latin-1"))
            else:
                q = R.tostr(a).decode("latin-1")
                q = q.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\0", "\\0")
                out.append('"' + q + '"')
        out.append(f[last:])
        return ["".join(out).encode("latin-1")]

    def s_find(s=None, pat=None, init=None, plain=None, *_):
        s = check_str(s, 1, "find")
        pat = check_str(pat, 2, "find")
        init = opt_int(init, 1, 3, "find")
        if init < 0:
            init = max(len(s) + init + 1, 1)
        elif init == 0:
            init = 1
        if init > len(s) + 1:
            return [None]
        if R.truthy(plain) or not re.search(rb"[\^$*+?.()\[\]%-]", pat):
            k = s.find(pat, init - 1)
            return [None] if k < 0 else [k + 1, k + len(pat)]
        rx, anchored, pc = pattern_to_regex(pat)
        m = find_match(rx, anchored, s, init - 1)
        if not m:
            return [None]
        return [m.start() + 1, m.end()] + captures_of(m, pc, False)

    def s_match(s=None, pat=None, init=None, *_):
        s = check_str(s, 1, "match")
        pat = check_str(pat, 2, "match")
        init = opt_int(init, 1, 3, "match")
        if init < 0:
            init = max(len(s) + init + 1, 1)
        elif init == 0:
            init = 1
        rx, anchored, pc = pattern_to_regex(pat)
        m = find_match(rx, anchored, s, init - 1)
        if not m:
            return [None]
        return captures_of(m, pc)

    def s_gmatch(s=None, pat=None, *_):
        s = check_str(s, 1, "gmatch")
        rx, anchored, pc = pattern_to_regex(check_str(pat, 2, "gmatch"))
        state = {"pos": 0}

        def it(*_):
            while state["pos"] <= len(s):
                m = rx.search(s, state["pos"])
                if not m:
                    state["pos"] = len(s) + 1
                    return [None]
                state["pos"] = m.end() if m.end() > m.start() else m.end() + 1
                return captures_of(m, pc)
            return [None]
        return [it]

    def s_gsub(s=None, pat=None, repl=None, maxn=None, *_):
        s = check_str(s, 1, "gsub")
        pat = check_str(pat, 2, "gsub")
        rx, anchored, pc = pattern_to_regex(pat)
        limit = None if maxn is None else check_int(maxn, 4, "gsub")
        out = []
        pos = 0
        count = 0
        while pos <= len(s) and (limit is None or count < limit):
            m = rx.match(s, pos) if anchored else rx.search(s, pos)
            if not m:
                break
            out.append(s[pos:m.start()])
            caps = captures_of(m, pc)
            whole = m.group(0)
            if repl.__class__ is bytes or repl.__class__ in (int, float):
                rs = R.tostr(repl)
                piece = re.sub(
                    rb"%(.)",
                    lambda mm: whole if mm.group(1) == b"0" else (
                        R.tostr(caps[int(mm.group(1)) - 1]) if mm.group(1).isdigit() else mm.group(1)
                    ),
                    rs,
                )
            elif repl.__class__ is LuaTable:
                v = R.index(repl, caps[0])
                piece = whole if not R.truthy(v) else R.tostr(v)
            else:
                r = R.call_value(repl, caps)
                v = r[0] if r else None
                piece = whole if not R.truthy(v) else R.tostr(v)
            out.append(piece)
            count += 1
            if m.end() == m.start():
                if m.start() < len(s):
                    out.append(s[m.start():m.start() + 1])
                pos = m.start() + 1
            else:
                pos = m.end()
            if anchored:
                break
        if pos <= len(s):
            out.append(s[pos:])
        return [b"".join(out), count]

    t = lib(len=s_len, sub=s_sub, byte=s_byte, char=s_char, upper=s_upper, lower=s_lower, rep=s_rep,
            reverse=s_reverse, format=s_format, find=s_find, match=s_match, gmatch=s_gmatch, gsub=s_gsub)
    R.STRING_META.d.update(t.d)
    return t


def build_math_lib():
    rng = random.Random(1337)

    def m_floor(x=None, *_):
        v = check_num(x, 1, "floor")
        if v.__class__ is int:
            return [v]
        if v != v or v in (math.inf, -math.inf):
            return [v]
        return [math.floor(v)]

    def m_ceil(x=None, *_):
        v = check_num(x, 1, "ceil")
        if v.__class__ is int or v != v or v in (math.inf, -math.inf):
            return [v]
        return [math.ceil(v)]

    def m_max(*a):
        if not a:
            raise err("bad argument #1 to 'max' (number expected, got no value)")
        vals = [check_num(x, i + 1, "max") for i, x in enumerate(a)]
        return [max(vals)]

    def m_min(*a):
        if not a:
            raise err("bad argument #1 to 'min' (number expected, got no value)")
        vals = [check_num(x, i + 1, "min") for i, x in enumerate(a)]
        return [min(vals)]

    def m_abs(x=None, *_):
        return [abs(check_num(x, 1, "abs"))]

    def m_sqrt(x=None, *_):
        v = check_num(x, 1, "sqrt")
        return [math.sqrt(v) if v >= 0 else math.nan]

    def m_fmod(a=None, b=None, *_):
        x = check_num(a, 1, "fmod")
        y = check_num(b, 2, "fmod")
        try:
            return [math.fmod(x, y)]
        except ValueError:
            return [math.nan]

    def m_pow(a=None, b=None, *_):
        return [R.ARITH["^"](check_num(a, 1, "pow"), check_num(b, 2, "pow"))]

    def m_random(a=None, b=None, *_):
        if a is None:
            return [rng.random()]
        lo, hi = (1, check_int(a, 1, "random")) if b is None else (check_int(a, 1, "random"), check_int(b, 2, "random"))
        if lo > hi:
            raise err("bad argument #2 to 'random' (interval is empty)")
        return [rng.randint(lo, hi)]

    def m_randomseed(x=None, *_):
        rng.seed(R.tostr(x) if x is not None else 0)
        return []

    def m_modf(x=None, *_):
        v = float(check_num(x, 1, "modf"))
        f, i = math.modf(v)
        return [float(i), f]

    def unary(fn, name):
        def run(x=None, *_):
            try:
                return [fn(float(check_num(x, 1, name)))]
            except (ValueError, OverflowError):
                return [math.nan]
        return run

    def m_log(x=None, base=None, *_):
        v = float(check_num(x, 1, "log"))
        try:
            if base is None:
                return [math.log(v)]
            return [math.log(v) / math.log(float(check_num(base, 2, "log")))]
        except ValueError:
            return [-math.inf if v == 0 else math.nan]
        except ZeroDivisionError:
            return [math.nan]

    return lib(
        floor=m_floor, ceil=m_ceil, max=m_max, min=m_min, abs=m_abs, sqrt=m_sqrt, fmod=m_fmod,
        pow=m_pow, random=m_random, randomseed=m_randomseed, modf=m_modf, log=m_log,
        exp=unary(math.exp, "exp"), sin=unary(math.sin, "sin"), cos=unary(math.cos, "cos"),
        tan=unary(math.tan, "tan"), huge=math.inf, pi=math.pi,
    )


def build_table_lib():
    def t_insert(t=None, *args):
        check_table(t, 1, "insert")
        if len(args) == 1:
            t.set(t.n + 1, args[0])
        elif len(args) == 2:
            pos = check_int(args[0], 2, "insert")
            n = t.n
            if pos < 1 or pos > n + 1:
                raise err("bad argument #2 to 'insert' (position out of bounds)")
            for i in range(n, pos - 1, -1):
                t.set(i + 1, t.d.get(i))
            t.set(pos, args[1])
        else:
            raise err("wrong number of arguments to 'insert'")
        return []

    def t_remove(t=None, pos=None, *_):
        check_table(t, 1, "remove")
        n = t.n
        if pos is None:
            if n == 0:
                return [None]
            v = t.d.get(n)
            t.set(n, None)
            return [v]
        pos = check_int(pos, 2, "remove")
        if n == 0 and pos in (0, n):
            return [t.d.get(pos)]
        if pos < 1 or pos > n + 1:
            raise err("bad argument #2 to 'remove' (position out of bounds)")
        v = t.d.get(pos)
        for i in range(pos, n):
            t.set(i, t.d.get(i + 1))
        if pos <= n:
            t.set(n, None)
        return [v]

    def t_concat(t=None, sep=None, i=None, j=None, *_):
        check_table(t, 1, "concat")
        sep = b"" if sep is None else check_str(sep, 2, "concat")
        i = opt_int(i, 1, 3, "concat")
        j = opt_int(j, t.n, 4, "concat")
        parts = []
        for k in range(i, j + 1):
            v = t.d.get(k)
            if v.__class__ not in (bytes, int, float):
                raise err("invalid value (at index %d) in table for 'concat'" % k)
            parts.append(R.tostr(v))
        return [sep.join(parts)]

    def t_unpack(t=None, i=None, j=None, *_):
        check_table(t, 1, "unpack")
        i = opt_int(i, 1, 2, "unpack")
        j = opt_int(j, t.n, 3, "unpack")
        if j - i >= 8000000:
            raise err("too many results to unpack")
        d = t.d
        return [d.get(k) for k in range(i, j + 1)]

    def t_sort(t=None, comp=None, *_):
        check_table(t, 1, "sort")
        vals = [t.d.get(k) for k in range(1, t.n + 1)]
        if comp is None:
            def cmp(a, b):
                return -1 if R.lt(a, b) else (1 if R.lt(b, a) else 0)
        else:
            def cmp(a, b):
                r = R.call_value(comp, [a, b])
                if r and R.truthy(r[0]):
                    return -1
                r = R.call_value(comp, [b, a])
                return 1 if r and R.truthy(r[0]) else 0
        vals.sort(key=functools.cmp_to_key(cmp))
        for k, v in enumerate(vals):
            t.set(k + 1, v)
        return []

    def t_getn(t=None, *_):
        return [check_table(t, 1, "getn").n]

    def t_pack(*a):
        t = table_from_list(a)
        t.set(b"n", len(a))
        return [t]

    return lib(insert=t_insert, remove=t_remove, concat=t_concat, unpack=t_unpack, sort=t_sort, getn=t_getn, pack=t_pack)


def build_bit32_lib():
    M = 0xFFFFFFFF

    def ints(args, fn):
        return [check_int(a, i + 1, fn) & M for i, a in enumerate(args)]

    def b_bxor(*a):
        r = 0
        for v in ints(a, "bxor"):
            r ^= v
        return [r]

    def b_band(*a):
        r = M
        for v in ints(a, "band"):
            r &= v
        return [r]

    def b_bor(*a):
        r = 0
        for v in ints(a, "bor"):
            r |= v
        return [r]

    def b_bnot(x=None, *_):
        return [~check_int(x, 1, "bnot") & M]

    def b_lshift(x=None, n=None, *_):
        x = check_int(x, 1, "lshift") & M
        n = check_int(n, 2, "lshift")
        if n < 0:
            return [x >> -n if -n < 32 else 0]
        return [(x << n) & M if n < 32 else 0]

    def b_rshift(x=None, n=None, *_):
        x = check_int(x, 1, "rshift") & M
        n = check_int(n, 2, "rshift")
        if n < 0:
            return [(x << -n) & M if -n < 32 else 0]
        return [x >> n if n < 32 else 0]

    return lib(bxor=b_bxor, band=b_band, bor=b_bor, bnot=b_bnot, lshift=b_lshift, rshift=b_rshift)


class Hook:
    def __init__(self):
        self.captures = []
        self.prints = []


def build_globals(hook):
    g = LuaTable()

    def stub(*_):
        return []

    def g_load(chunk=None, *_):
        if chunk.__class__ is bytes:
            hook.captures.append(chunk)
            return [stub]
        if callable(chunk):
            parts = []
            while True:
                r = chunk()
                piece = r[0] if r else None
                if piece is None or piece == b"":
                    break
                parts.append(check_str(piece))
            hook.captures.append(b"".join(parts))
            return [stub]
        raise err("bad argument #1 to 'load' (string expected, got %s)" % R.type_name(chunk))

    def g_type(*a):
        if not a:
            raise err("bad argument #1 to 'type' (value expected)")
        return [R.type_name(a[0]).encode()]

    def g_tostring(v=None, *_):
        return [R.tostr(v)]

    def g_tonumber(v=None, base=None, *_):
        if base is None:
            return [R.to_number(v)]
        try:
            return [int(check_str(v).decode("latin-1").strip(), check_int(base))]
        except ValueError:
            return [None]

    def g_pairs(t=None, *_):
        check_table(t, 1, "pairs")
        return [NEXT, t, None]

    def ipairs_iter(t, i):
        i = i + 1
        v = R.index(t, i)
        return [i, v] if v is not None else [None]

    def g_ipairs(t=None, *_):
        if t is None:
            raise err("bad argument #1 to 'ipairs' (table expected, got nil)")
        return [ipairs_iter, t, 0]

    def g_select(n=None, *args):
        if n == b"#":
            return [len(args)]
        i = check_int(n, 1, "select")
        if i < 0:
            i = len(args) + i + 1
            if i < 1:
                raise err("bad argument #1 to 'select' (index out of range)")
        elif i == 0:
            raise err("bad argument #1 to 'select' (index out of range)")
        return list(args[i - 1:])

    def g_rawget(t=None, k=None, *_):
        return [check_table(t, 1, "rawget").get(k)]

    def g_rawset(t=None, k=None, v=None, *_):
        check_table(t, 1, "rawset").set(k, v)
        return [t]

    def g_rawequal(a=None, b=None, *_):
        return [a is b or (a.__class__ is not LuaTable and R.lua_eq(a, b))]

    def g_rawlen(v=None, *_):
        return [v.n if v.__class__ is LuaTable else len(check_str(v))]

    def g_setmetatable(t=None, m=None, *_):
        check_table(t, 1, "setmetatable")
        t.meta = m if m is not None else None
        return [t]

    def g_getmetatable(t=None, *_):
        if t.__class__ is LuaTable:
            return [t.meta]
        if t.__class__ is bytes:
            s = LuaTable()
            s.set(b"__index", R.STRING_META)
            return [s]
        return [None]

    def g_error(msg=None, level=None, *_):
        raise LuaError(msg)

    def g_assert(v=None, msg=None, *rest):
        if not R.truthy(v):
            raise LuaError(msg if msg is not None else b"assertion failed!")
        return [v, msg] + list(rest) if msg is not None else [v]

    def g_pcall(f=None, *args):
        try:
            return [True] + list(R.call_value(f, args))
        except LuaError as e:
            return [False, e.value]
        except SandboxLimit:
            raise
        except ERRORS as e:
            return [False, str(e).encode("latin-1", "replace")]

    def g_xpcall(f=None, handler=None, *args):
        try:
            return [True] + list(R.call_value(f, args))
        except LuaError as e:
            return [False] + list(R.call_value(handler, [e.value]))
        except SandboxLimit:
            raise
        except ERRORS as e:
            return [False] + list(R.call_value(handler, [str(e).encode("latin-1", "replace")]))

    def g_print(*args):
        hook.prints.append(b"\t".join(R.tostr(a) for a in args))
        return []

    def g_getfenv(*_):
        return [g]

    def g_unpack(*a):
        return t_unpack(*a)

    table_lib = build_table_lib()
    t_unpack = table_lib.d[b"unpack"]

    def o_time(*_):
        return [1700000000]

    def o_clock(*_):
        return [0.0]

    reg(g, "_G", g)
    g.set(b"_VERSION", b"Lua 5.1")
    for name, fn in (
        ("loadstring", g_load), ("load", g_load), ("type", g_type), ("tostring", g_tostring),
        ("tonumber", g_tonumber), ("pairs", g_pairs), ("ipairs", g_ipairs), ("next", NEXT),
        ("select", g_select), ("rawget", g_rawget), ("rawset", g_rawset), ("rawequal", g_rawequal),
        ("rawlen", g_rawlen), ("setmetatable", g_setmetatable), ("getmetatable", g_getmetatable),
        ("error", g_error), ("assert", g_assert), ("pcall", g_pcall), ("xpcall", g_xpcall),
        ("print", g_print), ("warn", g_print), ("getfenv", g_getfenv), ("setfenv", g_getfenv),
        ("getgenv", g_getfenv), ("unpack", g_unpack),
    ):
        reg(g, name, fn)
    g.set(b"string", build_string_lib(g, hook))
    g.set(b"math", build_math_lib())
    g.set(b"table", table_lib)
    g.set(b"bit32", build_bit32_lib())
    g.set(b"bit", g.d[b"bit32"])
    g.set(b"os", lib(time=o_time, clock=o_clock))
    return g

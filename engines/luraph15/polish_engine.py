#!/usr/bin/env python3
"""polish.py -- post-devirtualization readability passes (AST based, via luau-ast).

    python polish.py <luau-ast> <in.lua> <out.lua> [--report <strings.txt>] [--no-rename]

Runs to a fixpoint, re-parsing after every round; every round must still parse or
it is rolled back, so the pass can never make the output worse than its input.

Passes
  1. fold        constant folding of pure expressions:
                   arithmetic / comparison / logic / concat / #"str"
                   bit32.*  (band bor bxor bnot lshift rshift arshift lrotate rrotate
                             extract replace btest countlz countrz)
                   math.floor ceil abs max min fmod huge pi
                   string.char byte rep sub reverse upper lower len format(%s %d %x %q)
                   ("lit"):method(...) on string literals, utf8.char, table.concat({..})
                   tostring / tonumber / type / typeof on constants
                   table.pack(K)[1] / select("#", ...) / { K }[1]
                 including through aliases (`local bxor = bit32.bxor`, `local char = string.char`)
  2. propagate   generic-named locals (n, v3, flag2, ...) bound once to a literal and
                 never written are inlined, so the junk chains Luraph leaves behind
                 (opaque key schedules, `table.pack` padding) collapse
  3. branches    `if <const> then` / `while false do` / `x and y` with constant sides
  4. strings     every string literal re-rendered readably: `"\72\101\108\108\111"`
                 -> `"Hello"`, UTF-8 kept as text, binary kept escaped
  5. clean       luaclean.py (dead pure statements / unread locals) to a fixpoint
  6. rename      generic local names from usage:
                   Instance.new + .Name = "X"         -> X
                   loadstring(game:HttpGet(url))()      -> library name (Rayfield, Fluent...)
                   Lib:CreateWindow / :CreateTab / :AddToggle("Aimbot") -> Window, aimbotToggle
                   local fn passed to Signal:Connect   -> onSignal
                   local fn stored as Callback = fn   -> onName
                   require(path.To.Module)            -> Module
  7. report      strings report: URLs, Discord webhooks, base64/hex payloads (decoded),
                 remote / HttpGet targets -- optional, plus a short header comment
"""
import base64
import bisect
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------- parsing


class Src:
    def __init__(self, data: bytes, ast_exe: str, tmp: str):
        self.data = data
        p = tmp
        with open(p, "wb") as f:
            f.write(data)
        proc = subprocess.run([ast_exe, p], capture_output=True)
        if proc.returncode:
            raise SyntaxError(proc.stderr.decode("latin1")[:400])
        self.root = json.loads(proc.stdout.decode("utf-8", "replace"))["root"]
        self.ls = [0]
        for i, b in enumerate(data):
            if b == 10:
                self.ls.append(i + 1)

    def off(self, lc):
        l, c = map(int, lc.split(","))
        if l >= len(self.ls):
            return len(self.data)
        return self.ls[l] + c

    def span(self, node, field="location"):
        a, b = node[field].split(" - ")
        return self.off(a), self.off(b)

    def text(self, node):
        a, b = self.span(node)
        return self.data[a:b]


def parse_ok(ast_exe, data, tmp):
    try:
        Src(data, ast_exe, tmp)
        return True
    except Exception:
        return False


def children(n):
    for k, v in n.items():
        if k in ("location", "local", "indexLocation", "argLocation"):
            continue
        if isinstance(v, dict) and "type" in v:
            yield k, v
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict) and "type" in x:
                    yield k, x
                elif isinstance(x, dict):            # table items {kind,key,value}
                    for kk in ("key", "value"):
                        if isinstance(x.get(kk), dict):
                            yield kk, x[kk]


def walk(n, fn, parent=None, field=None):
    if fn(n, parent, field) is False:
        return
    for f, c in children(n):
        walk(c, fn, n, f)


def lkey(local):
    return local.get("location")


# ------------------------------------------------------------- lua strings

ESC = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34, "'": 39, "\n": 10}


def decode_lua_string(raw: bytes):
    """Decode the source text of a string literal; None when unsupported."""
    if not raw:
        return None
    q = raw[:1]
    if q == b"[":
        m = re.match(rb"\[(=*)\[", raw)
        if not m:
            return None
        body = raw[m.end(): len(raw) - len(m.group(0))]
        if body.startswith(b"\r\n"):
            body = body[2:]
        elif body.startswith(b"\n"):
            body = body[1:]
        return body
    if q not in (b'"', b"'") or raw[-1:] != q:
        return None
    s = raw[1:-1]
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != 92:
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            return None
        e = chr(s[i])
        if e in ESC:
            out.append(ESC[e])
            i += 1
        elif e == "\r":
            out.append(10)
            i += 1
            if i < n and s[i] == 10:
                i += 1
        elif e.isdigit():
            j = i
            while j < n and j - i < 3 and chr(s[j]).isdigit():
                j += 1
            v = int(s[i:j])
            if v > 255:
                return None
            out.append(v)
            i = j
        elif e == "x":
            h = s[i + 1:i + 3]
            if len(h) != 2:
                return None
            out.append(int(h, 16))
            i += 3
        elif e == "z":
            i += 1
            while i < n and s[i] in b" \t\r\n\f\v":
                i += 1
        elif e == "u":
            m = re.match(rb"\{([0-9a-fA-F]+)\}", s[i + 1:])
            if not m:
                return None
            out += chr(int(m.group(1), 16)).encode("utf-8", "surrogatepass")
            i += 1 + m.end()
        else:
            return None
    return bytes(out)


def render_string(b: bytes) -> str:
    try:
        txt = b.decode("utf-8")
        utf = True
    except UnicodeDecodeError:
        utf = False
    q = '"'
    if utf and '"' in txt and "'" not in txt:
        q = "'"
    out = [q]
    if utf:
        for ch in txt:
            o = ord(ch)
            if ch == "\\":
                out.append("\\\\")
            elif ch == q:
                out.append("\\" + q)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif o < 32 or o == 127:
                out.append("\\%d" % o)
            elif 0x80 <= o < 0xA0 or o in (0x200B, 0x200C, 0x200D, 0x2028, 0x2029, 0xFEFF):
                out.append("\\u{%x}" % o)
            else:
                out.append(ch)
    else:
        for i, o in enumerate(b):
            ch = chr(o)
            if ch == "\\":
                out.append("\\\\")
            elif ch == q:
                out.append("\\" + q)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif 32 <= o < 127:
                out.append(ch)
            else:
                nxt = b[i + 1:i + 2]
                out.append("\\%03d" % o if nxt and nxt.isdigit() else "\\%d" % o)
    out.append(q)
    return "".join(out)


# ----------------------------------------------------------- const values


class NilT:
    def __repr__(self):
        return "nil"


NIL = NilT()
FAIL = object()
TWO32 = 4294967296.0


def is_num(v):
    return isinstance(v, float)


def truthy(v):
    return not (v is NIL or v is False)


def lua_type(v):
    if v is NIL:
        return "nil"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, float):
        return "number"
    if isinstance(v, bytes):
        return "string"
    return None


def u32(v):
    if not is_num(v) or v != v or v in (math.inf, -math.inf):
        raise ValueError
    if v != math.floor(v):
        raise ValueError
    return int(v) % 4294967296


def fnum(i):
    return float(i)


def lua_tostring_num(v):
    if v == math.floor(v) and abs(v) < 1e15:
        return str(int(v))
    r = "%.14g" % v
    return r


def lua_tonumber(b: bytes):
    s = b.decode("latin1").strip()
    try:
        if re.fullmatch(r"[-+]?0[xX][0-9a-fA-F]+", s):
            return float(int(s, 16))
        if re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s):
            return float(s)
    except ValueError:
        pass
    return NIL


def bit32_call(name, a):
    if name == "band":
        r = 0xFFFFFFFF
        for x in a:
            r &= u32(x)
        return fnum(r)
    if name == "bor":
        r = 0
        for x in a:
            r |= u32(x)
        return fnum(r)
    if name == "bxor":
        r = 0
        for x in a:
            r ^= u32(x)
        return fnum(r)
    if name == "btest":
        r = 0xFFFFFFFF
        for x in a:
            r &= u32(x)
        return r != 0
    if name == "bnot" and len(a) >= 1:
        return fnum(~u32(a[0]) & 0xFFFFFFFF)
    if len(a) < 2:
        raise ValueError
    x, d = u32(a[0]), a[1]
    if not is_num(d) or d != math.floor(d):
        raise ValueError
    d = int(d)
    if name == "lshift":
        return fnum((x << d) & 0xFFFFFFFF if 0 <= d < 32 else (x >> -d if -32 < d < 0 else 0))
    if name == "rshift":
        return fnum(x >> d if 0 <= d < 32 else ((x << -d) & 0xFFFFFFFF if -32 < d < 0 else 0))
    if name == "arshift":
        sx = x - 4294967296 if x & 0x80000000 else x
        if d >= 32:
            return fnum(0xFFFFFFFF if sx < 0 else 0)
        if d < 0:
            return fnum((x << -d) & 0xFFFFFFFF)
        return fnum((sx >> d) & 0xFFFFFFFF)
    if name == "lrotate":
        d %= 32
        return fnum(((x << d) | (x >> (32 - d))) & 0xFFFFFFFF)
    if name == "rrotate":
        d %= 32
        return fnum(((x >> d) | (x << (32 - d))) & 0xFFFFFFFF)
    if name == "extract":
        w = int(a[2]) if len(a) > 2 else 1
        if d < 0 or w < 1 or d + w > 32:
            raise ValueError
        return fnum((x >> d) & ((1 << w) - 1))
    if name == "replace" and len(a) >= 3:
        v = u32(a[1])
        f = a[2]
        w = int(a[3]) if len(a) > 3 else 1
        f = int(f)
        if f < 0 or w < 1 or f + w > 32:
            raise ValueError
        m = ((1 << w) - 1) << f
        return fnum((x & ~m | (v << f) & m) & 0xFFFFFFFF)
    if name == "countlz":
        return fnum(32 - x.bit_length())
    if name == "countrz":
        if x == 0:
            return 32.0
        return fnum((x & -x).bit_length() - 1)
    raise ValueError


def lua_format(fmt: bytes, args):
    out = bytearray()
    i = 0
    ai = 0
    while i < len(fmt):
        c = fmt[i]
        if c != 37:
            out.append(c)
            i += 1
            continue
        m = re.match(rb"%([-0 +#]*)(\d*)(\.\d+)?([sdixXqfg%])", fmt[i:])
        if not m:
            raise ValueError
        flags, width, prec, conv = m.groups()
        i += m.end()
        if conv == b"%":
            out.append(37)
            continue
        if ai >= len(args):
            raise ValueError
        a = args[ai]
        ai += 1
        spec = "%" + flags.decode() + width.decode() + (prec.decode() if prec else "")
        if conv == b"s":
            if isinstance(a, bytes):
                s = a
            elif is_num(a):
                s = lua_tostring_num(a).encode()
            elif isinstance(a, bool):
                s = b"true" if a else b"false"
            elif a is NIL:
                s = b"nil"
            else:
                raise ValueError
            out += (spec + "s").encode() % s if (flags or width or prec) else s
        elif conv in (b"d", b"i", b"x", b"X"):
            if not is_num(a) or a != math.floor(a):
                raise ValueError
            out += (spec + conv.decode().replace("i", "d")).encode() % int(a)
        elif conv in (b"f", b"g"):
            if not is_num(a):
                raise ValueError
            out += ((spec + conv.decode()) % a).encode()
        elif conv == b"q":
            if not isinstance(a, bytes):
                raise ValueError
            out += render_string(a).encode()
        else:
            raise ValueError
    return bytes(out)


def str_method(name, s, a):
    if name == "len":
        return fnum(len(s))
    if name == "upper":
        return s.upper()
    if name == "lower":
        return s.lower()
    if name == "reverse":
        return s[::-1]
    if name == "rep":
        n = a[0] if a else FAIL
        if not is_num(n) or n != math.floor(n) or n > 4096 or len(s) * max(n, 0) > 4096:
            raise ValueError
        sep = a[1] if len(a) > 1 else b""
        if not isinstance(sep, bytes):
            raise ValueError
        return sep.join([s] * int(n)) if n > 0 else b""
    if name == "sub":
        i = a[0] if a else 1.0
        j = a[1] if len(a) > 1 else -1.0
        if not (is_num(i) and is_num(j)):
            raise ValueError
        L = len(s)
        i, j = int(i), int(j)
        if i < 0:
            i = max(L + i + 1, 1)
        elif i == 0:
            i = 1
        if j < 0:
            j = L + j + 1
        elif j > L:
            j = L
        return s[i - 1:j] if i <= j else b""
    if name == "byte":
        i = a[0] if a else 1.0
        if not is_num(i) or len(a) > 1:
            raise ValueError
        i = int(i)
        if i < 0:
            i = len(s) + i + 1
        if not 1 <= i <= len(s):
            raise ValueError
        return fnum(s[i - 1])
    if name == "format":
        return lua_format(s, a)
    raise ValueError


LIBS = {
    "bit32": {"band", "bor", "bxor", "bnot", "lshift", "rshift", "arshift", "lrotate", "rrotate",
              "extract", "replace", "btest", "countlz", "countrz"},
    "math": {"floor", "ceil", "abs", "max", "min", "fmod"},
    "string": {"char", "byte", "rep", "sub", "reverse", "upper", "lower", "len", "format"},
    "utf8": {"char"},
}
GLOBAL_FNS = {"tostring", "tonumber", "type", "typeof"}
ALIAS_LIBS = {"bit32", "math", "string", "table", "coroutine", "task", "utf8", "buffer", "os", "debug"}
LIB_FN_NAMES = {"bxor", "band", "bor", "bnot", "lshift", "rshift", "arshift", "lrotate", "rrotate", "extract",
                "char", "byte", "sub", "gsub", "rep", "format", "concat", "insert", "remove", "unpack",
                "floor", "ceil", "abs", "max", "min", "random", "spawn", "wait", "defer", "delay",
                "readi8", "readu8", "readi16", "readu16", "readi32", "readu32", "readf32", "readf64",
                "writeu8", "writei8", "writeu32", "len", "pack", "find", "match", "gmatch", "upper", "lower",
                "yield", "wrap", "create", "resume", "clock", "time", "traceback", "huge", "pi", "sqrt"}


def call_builtin(lib, name, a):
    if lib == "bit32":
        return bit32_call(name, a)
    if lib == "math":
        if not all(is_num(x) for x in a) or not a:
            raise ValueError
        if name == "floor":
            return fnum(math.floor(a[0]))
        if name == "ceil":
            return fnum(math.ceil(a[0]))
        if name == "abs":
            return abs(a[0])
        if name == "max":
            return max(a)
        if name == "min":
            return min(a)
        if name == "fmod":
            if a[1] == 0:
                raise ValueError
            return math.fmod(a[0], a[1])
    if lib == "string":
        if name == "char":
            if not all(is_num(x) and 0 <= x <= 255 and x == math.floor(x) for x in a):
                raise ValueError
            return bytes(int(x) for x in a)
        if not a or not isinstance(a[0], bytes):
            raise ValueError
        return str_method(name, a[0], a[1:])
    if lib == "utf8" and name == "char":
        if not all(is_num(x) and 0 <= x < 0x110000 and x == math.floor(x) for x in a):
            raise ValueError
        return "".join(chr(int(x)) for x in a).encode("utf-8", "surrogatepass")
    if lib is None:
        if len(a) < 1:
            raise ValueError
        v = a[0]
        if name in ("type", "typeof"):
            t = lua_type(v)
            if t is None:
                raise ValueError
            return t.encode()
        if name == "tostring":
            if isinstance(v, bytes):
                return v
            if is_num(v):
                return lua_tostring_num(v).encode()
            if isinstance(v, bool):
                return b"true" if v else b"false"
            if v is NIL:
                return b"nil"
        if name == "tonumber" and len(a) == 1:
            if is_num(v):
                return v
            if isinstance(v, bytes):
                return lua_tonumber(v)
    raise ValueError


def arith(op, x, y):
    if op == "Concat":
        def cs(v):
            if isinstance(v, bytes):
                return v
            if is_num(v):
                return lua_tostring_num(v).encode()
            raise ValueError
        r = cs(x) + cs(y)
        if len(r) > 8192:
            raise ValueError
        return r
    if op in ("CompareEq", "CompareNe"):
        if lua_type(x) is None or lua_type(y) is None:
            raise ValueError
        eq = (lua_type(x) == lua_type(y)) and (x == y if x is not NIL else True)
        return eq if op == "CompareEq" else not eq
    if op in ("CompareLt", "CompareLe", "CompareGt", "CompareGe"):
        if not ((is_num(x) and is_num(y)) or (isinstance(x, bytes) and isinstance(y, bytes))):
            raise ValueError
        return {"CompareLt": x < y, "CompareLe": x <= y, "CompareGt": x > y, "CompareGe": x >= y}[op]
    if not (is_num(x) and is_num(y)):
        raise ValueError
    if op == "Add":
        return x + y
    if op == "Sub":
        return x - y
    if op == "Mul":
        return x * y
    if op == "Div":
        if y == 0:
            raise ValueError
        return x / y
    if op == "FloorDiv":
        if y == 0:
            raise ValueError
        return float(math.floor(x / y))
    if op == "Mod":
        if y == 0:
            raise ValueError
        return x - math.floor(x / y) * y
    if op == "Pow":
        r = x ** y
        if isinstance(r, complex):
            raise ValueError
        return float(r)
    raise ValueError


def num_ok(v):
    return is_num(v) and v == v and abs(v) != math.inf


def render_num(v):
    if v == math.floor(v) and abs(v) < 2 ** 53:
        return str(int(v))
    r = repr(v)
    if float(r) != v:
        raise ValueError
    return r


def render(v):
    if v is NIL:
        return "nil"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if is_num(v):
        if not num_ok(v):
            raise ValueError
        return render_num(v)
    if isinstance(v, bytes):
        if len(v) > 4096:
            raise ValueError
        return render_string(v)
    raise ValueError


# ------------------------------------------------------------- analysis

GENERIC = re.compile(r"^(v|n|fn|tbl|obj|flag|arg|val|res|str|num|bool|ok|t|x|u|upv|var|value)\d*$")


class Info:
    """Per-parse facts about locals."""

    def __init__(self, S: Src):
        self.S = S
        self.decl = {}         # key -> AstLocal node
        self.writes = {}       # key -> count
        self.reads = {}        # key -> [AstExprLocal nodes]
        self.read_parent = {}  # id(read node) -> (parent, field)
        self.init = {}         # key -> init expr (AstStatLocal 1:1 only)
        self.kind = {}         # key -> 'local' | 'param' | 'for' | 'func'
        self.idents = set()
        self.gwrites = set()
        root = S.root
        walk(root, self._visit)

    def _visit(self, n, parent, field):
        t = n["type"]
        if t == "AstStatLocal":
            vals = n.get("values", [])
            multi = bool(vals) and vals[-1]["type"] in ("AstExprCall", "AstExprVarargs")
            for i, v in enumerate(n["vars"]):
                k = lkey(v)
                self.decl[k] = v
                self.kind[k] = "local"
                self.idents.add(v["name"])
                if i < len(vals) and not (multi and i == len(vals) - 1 and len(vals) < len(n["vars"])):
                    self.init[k] = vals[i]
                elif i >= len(vals) and not multi:
                    self.init[k] = {"type": "AstExprConstantNil"}
        elif t in ("AstExprFunction",):
            for a in n.get("args", []):
                self.decl[lkey(a)] = a
                self.kind[lkey(a)] = "param"
                self.idents.add(a["name"])
        elif t in ("AstStatFor",):
            v = n["var"]
            self.decl[lkey(v)] = v
            self.kind[lkey(v)] = "for"
            self.idents.add(v["name"])
        elif t == "AstStatForIn":
            for v in n["vars"]:
                self.decl[lkey(v)] = v
                self.kind[lkey(v)] = "for"
                self.idents.add(v["name"])
        elif t == "AstStatLocalFunction":
            v = n["name"]
            self.decl[lkey(v)] = v
            self.kind[lkey(v)] = "func"
            self.idents.add(v["name"])
        elif t in ("AstStatAssign", "AstStatCompoundAssign"):
            tg = n["vars"] if t == "AstStatAssign" else [n["var"]]
            for x in tg:
                if x["type"] == "AstExprLocal":
                    k = lkey(x["local"])
                    self.writes[k] = self.writes.get(k, 0) + 1
                    x["_target"] = True
                elif x["type"] == "AstExprGlobal":
                    self.gwrites.add(x["global"])
        elif t == "AstExprLocal":
            if not n.get("_target"):
                self.reads.setdefault(lkey(n["local"]), []).append(n)
                self.read_parent[id(n)] = (parent, field)
        elif t == "AstExprGlobal":
            self.idents.add(n["global"])


def lit_value(S, e):
    t = e["type"]
    if t == "AstExprConstantNumber":
        v = e["value"]
        if not isinstance(v, (int, float)):
            return FAIL
        return float(v)
    if t == "AstExprConstantBool":
        return bool(e["value"])
    if t == "AstExprConstantNil":
        return NIL
    if t == "AstExprConstantString":
        b = decode_lua_string(S.text(e))
        return FAIL if b is None else b
    return FAIL


class Folder:
    def __init__(self, S: Src, I: Info, propagate=True):
        self.S, self.I = S, I
        self.propagate = propagate
        self.aliases = {}     # key -> (lib, fn)
        self.consts = {}      # key -> value
        self.packs = {}       # key -> value  (local t = table.pack(K) / {K}; only t[1] reads)
        self.galias = {}      # key -> global path text (`local v = print` -> print)
        self._prep()

    def _prep(self):
        I = self.I
        for k, init in I.init.items():
            if I.writes.get(k) or I.kind.get(k) != "local":
                continue
            t = init.get("type")
            if t == "AstExprIndexName" and init["expr"]["type"] == "AstExprGlobal":
                g, f = init["expr"]["global"], init["index"]
                if f in LIBS.get(g, ()):
                    self.aliases[k] = (g, f)
            elif t == "AstExprGlobal" and init["global"] in GLOBAL_FNS:
                self.aliases[k] = (None, init["global"])
        local_names = {d["name"] for d in I.decl.values()}
        for k, init in I.init.items():
            if I.writes.get(k) or I.kind.get(k) != "local" or not init.get("location"):
                continue
            if not GENERIC.match(I.decl[k]["name"]) and I.decl[k]["name"] not in LIB_FN_NAMES:
                continue
            g = None
            if init["type"] == "AstExprGlobal":
                g = init["global"]
            elif init["type"] == "AstExprIndexName" and init["expr"]["type"] == "AstExprGlobal" and \
                    init["op"] == "." and init["expr"]["global"] in ALIAS_LIBS:
                g = init["expr"]["global"]
            if g and g not in local_names and g not in I.gwrites and re.match(r"^[A-Za-z_]\w*$", g):
                self.galias[k] = self.S.text(init).decode("latin1")
        for k, init in I.init.items():
            if I.writes.get(k) or I.kind.get(k) != "local":
                continue
            name = I.decl[k]["name"]
            if not GENERIC.match(name) or not self.propagate:
                pass
            elif init.get("location") is not None or init["type"] == "AstExprConstantNil":
                v = self.ev(init) if init.get("location") else NIL
                nreads = len(I.reads.get(k, []))
                # inline only what is junk-like: single use, Luraph key-schedule sized
                # numbers, or nil/bool; a small number used in several places
                # (`local n = 40` = a max size) keeps its name
                junk = nreads <= 1 or v is NIL or isinstance(v, bool) or \
                    (is_num(v) and num_ok(v) and abs(v) >= 65536)
                if v is not FAIL and junk and (not isinstance(v, bytes) or len(v) <= 64) and \
                        (not is_num(v) or num_ok(v)):
                    self.consts[k] = v
            # table.pack(K) / {K} read only as t[1]
            if init.get("location") is not None:
                pv = self._pack_val(init)
                if pv is not FAIL:
                    rs = I.reads.get(k, [])
                    ok = True
                    for r in rs:
                        par, fld = I.read_parent[id(r)]
                        if not (par["type"] == "AstExprIndexExpr" and fld == "expr" and
                                par["index"]["type"] == "AstExprConstantNumber" and par["index"]["value"] == 1):
                            ok = False
                            break
                    if ok:
                        self.packs[k] = pv

    def _pack_val(self, e):
        t = e["type"]
        if t == "AstExprCall" and not e.get("self") and len(e["args"]) == 1:
            f = e["func"]
            if f["type"] == "AstExprIndexName" and f["expr"]["type"] == "AstExprGlobal" and \
                    f["expr"]["global"] == "table" and f["index"] == "pack":
                if e["args"][0]["type"] in ("AstExprCall", "AstExprVarargs"):
                    return FAIL
                return self.ev(e["args"][0])
        if t == "AstExprTable" and len(e.get("items", [])) == 1:
            it = e["items"][0]
            if it.get("kind") == "item" and it["value"]["type"] not in ("AstExprCall", "AstExprVarargs"):
                return self.ev(it["value"])
        return FAIL

    def callee(self, f):
        t = f["type"]
        if t == "AstExprIndexName" and f["expr"]["type"] == "AstExprGlobal":
            g, n = f["expr"]["global"], f["index"]
            if n in LIBS.get(g, ()):
                return (g, n)
        if t == "AstExprGlobal" and f["global"] in GLOBAL_FNS:
            return (None, f["global"])
        if t == "AstExprLocal":
            return self.aliases.get(lkey(f["local"]))
        return None

    def ev(self, e):
        try:
            return self._ev(e)
        except (ValueError, OverflowError, ZeroDivisionError, TypeError, RecursionError, KeyError, IndexError):
            return FAIL

    def _ev(self, e):
        t = e["type"]
        v = lit_value(self.S, e)
        if v is not FAIL:
            return v
        if t == "AstExprGroup":
            return self._ev(e["expr"])
        if t == "AstExprLocal":
            k = lkey(e["local"])
            if k in self.consts:
                return self.consts[k]
            return FAIL
        if t == "AstExprUnary":
            x = self._ev(e["expr"])
            if x is FAIL:
                return FAIL
            op = e["op"]
            if op == "Not":
                return not truthy(x)
            if op == "Minus":
                if not is_num(x):
                    raise ValueError
                return -x
            if op == "Len":
                if not isinstance(x, bytes):
                    raise ValueError
                return fnum(len(x))
            return FAIL
        if t == "AstExprBinary":
            op = e["op"]
            x = self._ev(e["left"])
            if x is FAIL:
                return FAIL
            if op == "And":
                if not truthy(x):
                    return x
                return self._ev(e["right"])
            if op == "Or":
                if truthy(x):
                    return x
                return self._ev(e["right"])
            y = self._ev(e["right"])
            if y is FAIL:
                return FAIL
            r = arith(op, x, y)
            if is_num(r) and not num_ok(r):
                raise ValueError
            return r
        if t == "AstExprIfElse":
            c = self._ev(e["condition"])
            if c is FAIL:
                return FAIL
            return self._ev(e["trueExpr"] if truthy(c) else e["falseExpr"])
        if t == "AstExprIndexExpr":
            base = e["expr"]
            idx = e["index"]
            if idx["type"] == "AstExprConstantNumber" and idx["value"] == 1:
                if base["type"] == "AstExprLocal" and lkey(base["local"]) in self.packs:
                    return self.packs[lkey(base["local"])]
                b = base["expr"] if base["type"] == "AstExprGroup" else base
                pv = self._pack_val(b)
                if pv is not FAIL:
                    return pv
            return FAIL
        if t == "AstExprCall":
            f = e["func"]
            args = e["args"]
            if any(a["type"] == "AstExprVarargs" for a in args):
                return FAIL
            if e.get("self"):
                if f["type"] != "AstExprIndexName":
                    return FAIL
                s = self._ev(f["expr"])
                if not isinstance(s, bytes) or f["index"] not in LIBS["string"] - {"char"}:
                    return FAIL
                a = [self._ev(x) for x in args]
                if any(x is FAIL for x in a):
                    return FAIL
                return str_method(f["index"], s, a)
            c = self.callee(f)
            if c is None:
                # table.concat({"a","b"}, sep)
                if f["type"] == "AstExprIndexName" and f["expr"]["type"] == "AstExprGlobal" and \
                        f["expr"]["global"] == "table" and f["index"] == "concat" and args and \
                        args[0]["type"] == "AstExprTable":
                    parts = []
                    for it in args[0].get("items", []):
                        if it.get("kind") != "item":
                            return FAIL
                        v = self._ev(it["value"])
                        if is_num(v):
                            v = lua_tostring_num(v).encode()
                        if not isinstance(v, bytes):
                            return FAIL
                        parts.append(v)
                    sep = self._ev(args[1]) if len(args) > 1 else b""
                    if not isinstance(sep, bytes) or len(args) > 2:
                        return FAIL
                    return sep.join(parts)
                if f["type"] == "AstExprGlobal" and f["global"] == "select" and args:
                    s0 = self._ev(args[0])
                    if s0 == b"#" and all(a["type"] not in ("AstExprCall",) for a in args[1:]):
                        return fnum(len(args) - 1)
                return FAIL
            # last arg a call -> multiple values, unknown
            if args and args[-1]["type"] == "AstExprCall":
                return FAIL
            a = [self._ev(x) for x in args]
            if any(x is FAIL for x in a):
                return FAIL
            r = call_builtin(c[0], c[1], a)
            if is_num(r) and not num_ok(r):
                raise ValueError
            return r
        if t == "AstExprIndexName" and e["expr"]["type"] == "AstExprGlobal" and e["expr"]["global"] == "math":
            if e["index"] == "pi":
                return math.pi
        return FAIL


# ------------------------------------------------------------ rewriting

LITERAL_TYPES = {"AstExprConstantNumber", "AstExprConstantString", "AstExprConstantBool", "AstExprConstantNil"}


def fold_round(S: Src, propagate=True, strings=True):
    I = Info(S)
    F = Folder(S, I, propagate)
    # branch conditions are judged WITHOUT propagated locals: `local flag = false
    # ... if flag then <code> end` stays visible (the devirtualizer may have lost the
    # write that sets it, and for an audit tool hidden code is worse than junk);
    # only literal opaque predicates (`if not nil`, `if 1 == 2`) are resolved.
    FC = Folder(S, I, False)
    edits = []   # (a, b, text)
    stats = {"folded": 0, "strings": 0, "branches": 0}

    def needs_paren(parent, field, txt):
        if parent is None:
            return False
        pt = parent["type"]
        if pt in ("AstExprIndexName", "AstExprIndexExpr", "AstExprCall") and field in ("expr", "func"):
            return True
        if txt.startswith("-") and pt == "AstExprBinary" and parent["op"] == "Pow" and field == "left":
            return True
        if txt.startswith("-") and pt == "AstExprUnary":
            return True
        return False

    def visit(n, parent, field):
        t = n["type"]
        if t.startswith("AstExpr") and not n.get("_target"):
            if t in LITERAL_TYPES:
                if t == "AstExprConstantString" and strings:
                    raw = S.text(n)
                    b = decode_lua_string(raw)
                    if b is not None:
                        nt = render_string(b)
                        if nt.encode("utf-8") != raw and (raw[:1] in (b'"', b"'")) and \
                                (b"\\" in raw or nt.count("\\") < raw.count(b"\\")):
                            a, z = S.span(n)
                            edits.append((a, z, nt))
                            stats["strings"] += 1
                return False
            if t in ("AstExprFunction", "AstExprTable", "AstExprVarargs", "AstExprGlobal",
                     "AstExprInterpString"):
                return True
            if t == "AstExprLocal" and propagate and lkey(n["local"]) in F.galias:
                a, z = S.span(n)
                edits.append((a, z, F.galias[lkey(n["local"])]))
                stats["folded"] += 1
                return False
            if t == "AstExprLocal" and parent is not None and parent["type"] == "AstExprCall" and field == "func":
                return False
            v = F.ev(n)
            if v is not FAIL:
                try:
                    txt = render(v)
                except ValueError:
                    return True
                if needs_paren(parent, field, txt):
                    txt = "(" + txt + ")"
                a, z = S.span(n)
                edits.append((a, z, txt))
                stats["folded"] += 1
                return False
            return True
        if t == "AstStatIf":
            c = FC.ev(n["condition"])
            if c is FAIL and bare_local(n["condition"]):
                walk_inner_skip(n, "condition")
                return False
            if c is not FAIL:
                a, z = S.span(n)
                if truthy(c):
                    edits.append((a, z, as_do(n["thenbody"], a)))
                    stats["branches"] += 1
                    walk_inner(n["thenbody"])
                    return False
                eb = n.get("elsebody")
                if eb is None:
                    edits.append((a, z, ""))
                    stats["branches"] += 1
                    return False
                if eb["type"] == "AstStatBlock":
                    edits.append((a, z, as_do(eb, a)))
                    stats["branches"] += 1
                    walk_inner(eb)
                    return False
            return True
        if t == "AstStatWhile":
            c = FC.ev(n["condition"])
            if c is FAIL and bare_local(n["condition"]):
                walk_inner_skip(n, "condition")
                return False
            if c is not FAIL and not truthy(c):
                a, z = S.span(n)
                edits.append((a, z, ""))
                stats["branches"] += 1
                return False
            return True
        if t == "AstStatAssign":
            for v in n["vars"]:
                if v["type"] != "AstExprLocal":
                    walk_target(v)
            for v in n["values"]:
                walk(v, visit, n, "values")
            return False
        if t == "AstStatCompoundAssign":
            if n["var"]["type"] != "AstExprLocal":
                walk_target(n["var"])
            walk(n["value"], visit, n, "value")
            return False
        return True

    def bare_local(e):
        while e["type"] in ("AstExprGroup", "AstExprUnary") and (e["type"] == "AstExprGroup" or e["op"] == "Not"):
            e = e["expr"]
        return e["type"] == "AstExprLocal"

    def walk_inner_skip(n, skip):
        for f, c in children(n):
            if f != skip:
                walk(c, visit, n, f)

    def as_do(block, a):
        body = S.text(block).decode("utf-8", "replace")
        if not body.strip():
            return ""
        ls = S.ls[bisect.bisect_right(S.ls, a) - 1]
        ind = re.match(r"[ \t]*", S.data[ls:a].decode("latin1")).group(0)
        if not body.endswith("\n"):
            body += "\n"
        return "do" + body + ind + "end"

    def walk_target(v):
        # x.y = / x[k] = : fold inside the key/base but never the target itself
        for f, c in children(v):
            walk(c, visit, v, f)

    def walk_inner(block):
        for f, c in children(block):
            walk(c, visit, block, f)

    walk(S.root, visit)
    return apply_edits(S.data, edits), stats


def apply_edits(data: bytes, edits):
    fixed = []
    for a, z, txt in edits:
        if txt == "":
            ls = data.rfind(b"\n", 0, a) + 1
            le = data.find(b"\n", z)
            le = len(data) if le < 0 else le
            if not data[ls:a].strip() and not data[z:le].strip():
                a, z = ls, min(le + 1, len(data))
        fixed.append((a, z, txt))
    edits = fixed
    edits.sort(key=lambda e: (e[0], -e[1]))
    out = bytearray()
    pos = 0
    last_end = -1
    for a, z, txt in edits:
        if a < last_end:     # nested / overlapping: keep the outer one
            continue
        out += data[pos:a]
        out += txt.encode("utf-8") if isinstance(txt, str) else txt
        pos = z
        last_end = z
    out += data[pos:]
    return bytes(out)


# --------------------------------------------------------------- renaming

KNOWN_LIBS = [
    ("rayfield", "Rayfield"), ("orion", "OrionLib"), ("fluent", "Fluent"), ("kavo", "Kavo"),
    ("linoria", "Library"), ("obsidian", "Library"), ("venyx", "Venyx"), ("wally", "Library"),
    ("mercury", "Mercury"), ("windui", "WindUI"), ("wind-ui", "WindUI"), ("splib", "SPLib"),
    ("luna", "Luna"), ("vape", "Vape"), ("material", "Material"), ("turtle", "TurtleLib"),
    ("savemanager", "SaveManager"), ("interfacemanager", "InterfaceManager"),
    ("thememanager", "ThemeManager"), ("esp", "ESP"), ("sense", "Sense"),
]
RESERVED = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in", "local",
    "nil", "not", "or", "repeat", "return", "then", "true", "until", "while", "continue", "type",
    "game", "workspace", "script", "self", "string", "table", "math", "task", "Instance", "Enum",
}


# what a call hands back, used to name the register it lands in
VALUE_BY_METHOD = {
    "GetChildren": "children", "GetDescendants": "descendants", "GetPlayers": "players",
    "GetService": "service", "Clone": "clone", "Connect": "connection", "Once": "connection",
    "GetPropertyChangedSignal": "signal", "GetMouse": "mouse", "Raycast": "hit",
    "InvokeServer": "response", "GetAsync": "response", "HttpPost": "response",
    "JSONDecode": "data", "JSONEncode": "json", "GetAttribute": "attribute",
    "FindFirstChild": "child", "FindFirstChildOfClass": "child", "WaitForChild": "child",
    "GetData": "data", "GetInfo": "info",
}
VALUE_BY_CALL = {
    "tick": "now", "time": "now", "typeof": "kind", "type": "kind", "tostring": "str",
    "tonumber": "num", "pairs": "iterator", "ipairs": "iterator", "next": "iterator",
    "setmetatable": "obj", "loadstring": "chunk", "getgenv": "genv", "getfenv": "env",
    "gethui": "hui", "newproxy": "proxy", "rawget": "value", "require": "module",
}


def singular(s):
    """Players -> player, Characters -> character (only a plain plural)."""
    if not s:
        return s
    low = s.lower()
    if len(s) > 3 and low.endswith("ies"):
        return s[:-3] + "y"
    if len(s) > 4 and low.endswith("ses"):
        return s[:-2]
    if len(s) > 2 and low.endswith("s") and not low.endswith("ss"):
        return s[:-1]
    return s


def ident(s, cap=False):
    words = re.findall(r"[A-Za-z0-9]+", s)
    if not words:
        return None
    w = "".join(x[:1].upper() + x[1:] for x in words)
    if not cap:
        w = w[:1].lower() + w[1:]
    if w[:1].isdigit():
        w = "_" + w
    if len(w) > 32:
        w = w[:32]
    return w


def lib_from_url(url):
    low = url.lower()
    for k, n in KNOWN_LIBS:
        if k in low:
            return n
    m = re.search(r"/([A-Za-z][A-Za-z0-9_\-]{2,30})(?:\.lua[u]?)?/?$", url)
    if m and m.group(1).lower() not in ("main", "source", "init", "script", "loader", "raw", "refs", "heads"):
        return ident(m.group(1), cap=True)
    return None


def rec_key(S, k):
    """record key of a table item (`Name = x` has no quotes in the source)."""
    v = k.get("value")
    if isinstance(v, str):
        return v.encode("utf-8")
    return decode_lua_string(S.text(k))


def str_arg(S, e):
    if e and e["type"] == "AstExprConstantString":
        b = decode_lua_string(S.text(e))
        if b is not None:
            try:
                return b.decode("utf-8")
            except UnicodeDecodeError:
                return None
    if e and e["type"] == "AstExprTable":
        for it in e.get("items", []):
            if it.get("kind") == "record" and it["key"]["type"] == "AstExprConstantString":
                kb = rec_key(S, it["key"])
                if kb in (b"Name", b"Title", b"Text", b"Flag"):
                    v = str_arg(S, it["value"])
                    if v:
                        return v
    return None


def suggest_names(S: Src, I: Info):
    sug = {}   # key -> name

    def generic(k):
        d = I.decl.get(k)
        return d is not None and GENERIC.match(d["name"]) and I.kind.get(k) in ("local", "func")

    def init_of(k):
        return I.init.get(k)

    # locals declared without a value and assigned exactly once elsewhere
    # (`local v ... v = Lib:CreateWindow{}`) are named from that value too
    inits = dict(I.init)
    one_assign = {}

    def collect_assign(n, parent, field):
        if n["type"] == "AstStatAssign" and len(n["vars"]) == 1 and len(n["values"]) == 1 \
                and n["vars"][0]["type"] == "AstExprLocal":
            k = lkey(n["vars"][0]["local"])
            one_assign.setdefault(k, []).append(n["values"][0])
        return True

    walk(S.root, collect_assign)
    for k, vals in one_assign.items():
        cur = inits.get(k)
        if len(vals) == 1 and I.writes.get(k, 0) == 1 and (cur is None or cur.get("type") == "AstExprConstantNil"):
            inits[k] = vals[0]

    for k, init in list(inits.items()):
        if not generic(k) or not init.get("location"):
            continue
        e = init
        # loadstring(game:HttpGet(url))()
        if e["type"] == "AstExprCall" and e["func"]["type"] == "AstExprCall":
            inner = e["func"]
            if inner["func"]["type"] == "AstExprGlobal" and inner["func"]["global"] in ("loadstring", "load") \
                    and inner["args"] and inner["args"][0]["type"] == "AstExprCall":
                hg = inner["args"][0]
                if hg.get("self") and hg["func"]["index"] in ("HttpGet", "HttpGetAsync") and hg["args"]:
                    u = str_arg(S, hg["args"][0])
                    if u:
                        n = lib_from_url(u)
                        if n:
                            sug[k] = n
                            continue
        if e["type"] == "AstExprCall" and e.get("self") and e["func"]["type"] == "AstExprIndexName":
            meth = e["func"]["index"]
            m = re.match(r"^(Create|Add|Make|New)([A-Z][A-Za-z]+)$", meth)
            if m:
                kind = m.group(2)
                title = str_arg(S, e["args"][0]) if e["args"] else None
                if kind in ("Window", "Lib", "Library"):
                    sug[k] = "Window" if kind == "Window" else "Library"
                else:
                    base = ident(title) if title else None
                    if base and len(base) <= 24:
                        sug[k] = base + kind
                    else:
                        sug[k] = kind[:1].lower() + kind[1:]
                continue
            if meth in ("FindFirstChild", "WaitForChild", "FindFirstChildOfClass", "FindFirstChildWhichIsA") and e["args"]:
                s = str_arg(S, e["args"][0])
                if s:
                    nm = ident(s)
                    if nm:
                        sug[k] = nm
                        continue
        if e["type"] == "AstExprCall" and e["func"]["type"] == "AstExprGlobal" and e["func"]["global"] == "require" \
                and e["args"] and e["args"][0]["type"] == "AstExprIndexName":
            sug[k] = ident(e["args"][0]["index"], cap=True)
            continue
        if e["type"] == "AstExprCall" and e["func"]["type"] == "AstExprIndexName" and \
                e["func"]["expr"]["type"] == "AstExprGlobal" and e["func"]["expr"]["global"] == "Instance" and \
                e["func"]["index"] == "new" and e["args"]:
            cls = str_arg(S, e["args"][0])
            if cls:
                sug.setdefault(k, ident(cls))

    # tiny wrappers: `local function fn(a) return game:HttpGet(a) end` -> httpGet
    SKIP_CALLS = {"pcall", "xpcall", "select", "unpack", "setmetatable", "rawget", "rawset", "type",
                  "typeof", "tostring", "tonumber", "assert", "error", "next", "ipairs", "pairs"}

    def fn_expr_of(k):
        d = I.decl.get(k)
        e = I.init.get(k)
        if e is None:
            return fdefs.get(k)
        if e.get("type") == "AstExprFunction":
            return e
        if e.get("type") == "AstExprBinary" and e["op"] in ("And", "Or") and \
                e["right"].get("type") == "AstExprFunction":
            return e["right"]
        return None

    fdefs = {}

    def collect_fdefs(n, parent, field):
        if n["type"] == "AstStatLocalFunction":
            fdefs[lkey(n["name"])] = n["func"]
        return True

    walk(S.root, collect_fdefs)
    for k in list(I.decl):
        if not generic(k) or k in sug:
            continue
        f = fn_expr_of(k)
        if not f:
            continue
        body = f["body"].get("body", [])
        if len(body) != 1 or body[0]["type"] != "AstStatReturn" or len(body[0].get("list", [])) != 1:
            continue
        c = body[0]["list"][0]
        if c["type"] != "AstExprCall":
            continue
        fn = c["func"]
        nm = None
        if fn["type"] == "AstExprIndexName":
            nm = fn["index"]
        elif fn["type"] == "AstExprGlobal" and fn["global"] not in SKIP_CALLS:
            nm = fn["global"]
        if nm and len(nm) >= 3:
            nm = ident(nm)
            if nm:
                sug[k] = nm

    # X.Name = "Str" for Instance-created locals
    def visit(n, parent, field):
        if n["type"] == "AstStatAssign" and len(n["vars"]) == 1 and len(n["values"]) == 1:
            tg = n["vars"][0]
            if tg["type"] == "AstExprIndexName" and tg["index"] == "Name" and tg["expr"]["type"] == "AstExprLocal":
                k = lkey(tg["expr"]["local"])
                if generic(k) or (k in sug and init_of(k) is not None):
                    s = str_arg(S, n["values"][0])
                    nm = ident(s) if s else None
                    if nm and len(nm) >= 3:
                        sug[k] = nm
        if n["type"] == "AstExprCall" and n.get("self") and n["func"]["type"] == "AstExprIndexName" and \
                n["func"]["index"] in ("Connect", "Once", "ConnectParallel") and n["args"] and \
                n["args"][0]["type"] == "AstExprLocal":
            k = lkey(n["args"][0]["local"])
            sig = n["func"]["expr"]
            if generic(k) and sig["type"] == "AstExprIndexName":
                sug.setdefault(k, "on" + ident(sig["index"], cap=True))
        if n["type"] == "AstExprTable":
            title = None
            cb = None
            for it in n.get("items", []):
                if it.get("kind") != "record" or it["key"]["type"] != "AstExprConstantString":
                    continue
                kb = rec_key(S, it["key"])
                if kb in (b"Name", b"Title", b"Text"):
                    title = str_arg(S, it["value"])
                elif kb == b"Callback" and it["value"]["type"] == "AstExprLocal":
                    cb = lkey(it["value"]["local"])
            if cb and title and generic(cb):
                nm = ident(title, cap=True)
                if nm:
                    sug[cb] = "on" + nm
            # UI element whose Callback stores its value into an upvalue:
            #   AddToggle{ Name = "Auto Block", Flag = "auto_block", Callback = function(v) flag14 = v end }
            #   -> flag14 becomes autoBlock
            label = None
            cbfun = None
            for it in n.get("items", []):
                if it.get("kind") != "record" or it["key"]["type"] != "AstExprConstantString":
                    continue
                kb = rec_key(S, it["key"])
                if kb == b"Flag":
                    label = str_arg(S, it["value"]) or label
                elif kb in (b"Name", b"Title", b"Text") and label is None:
                    label = label_arg(S, it["value"])
                elif kb in (b"Callback", b"OnChanged", b"Changed"):
                    cbfun = callback_fn(it["value"])
            if label and cbfun:
                tgt = stored_param(cbfun)
                if tgt and generic(tgt) and tgt not in sug:
                    nm = ident(re.sub(r"(?i)(toggle|slider|dropdown|input|keybind|button)$", "", label) or label)
                    if nm and len(nm) >= 3:
                        sug[tgt] = nm
        return True

    walk(S.root, visit)

    # localisation helper: fn(k) looks k up in a table and falls back to k,
    # called many times with one string literal -> translate
    calls = {}

    def count_calls(n, parent, field):
        if n["type"] == "AstExprCall" and not n.get("self") and n["func"]["type"] == "AstExprLocal" \
                and len(n["args"]) == 1 and n["args"][0]["type"] == "AstExprConstantString":
            k = lkey(n["func"]["local"])
            calls[k] = calls.get(k, 0) + 1
        return True

    walk(S.root, count_calls)
    for k, c in calls.items():
        if c < 10 or not generic(k) or k in sug:
            continue
        f = inits.get(k) or fdefs.get(k)
        if not f or f.get("type") != "AstExprFunction" or len(f.get("args", [])) != 1:
            continue
        pk = lkey(f["args"][0])
        seen = {"ret": False, "idx": False}

        def look(n, parent, field):
            if n["type"] == "AstStatReturn" and n.get("list") and n["list"][0]["type"] == "AstExprLocal" \
                    and lkey(n["list"][0]["local"]) == pk:
                seen["ret"] = True
            if n["type"] == "AstExprIndexExpr" and n["index"]["type"] == "AstExprLocal" \
                    and lkey(n["index"]["local"]) == pk:
                seen["idx"] = True
            return True

        walk(f["body"], look)
        if seen["ret"] and seen["idx"]:
            sug[k] = "translate"

    # fallback: when nothing better was found, take the name from the value the
    # register was loaded with. `local v15 = task` -> task, `local v = Random` ->
    # random, `local n = game:GetService("Players")` -> player. Only values whose
    # meaning is unambiguous are used, so a wrong label never replaces a generic
    # one, and a count/flag (arithmetic, comparison) keeps its generic name.
    for k, init in list(inits.items()):
        if k in sug or not generic(k) or not init.get("location"):
            continue
        e = init
        nm = None
        if e["type"] == "AstExprGlobal":
            g = e["global"]
            if g not in RESERVED and len(g) >= 3:
                nm = ident(g)
        elif e["type"] == "AstExprIndexName":
            nm = ident(e["index"]) if len(e["index"]) >= 4 else None
        elif e["type"] == "AstExprCall":
            f = e["func"]
            if f["type"] == "AstExprIndexName" and f.get("self") and f["index"] == "GetService" and e["args"]:
                s = str_arg(S, e["args"][0])
                nm = ident(singular(s)) if s else None
            elif f["type"] == "AstExprIndexName":
                nm = VALUE_BY_METHOD.get(f["index"])
                if nm is None and len(f["index"]) >= 4:
                    nm = ident(f["index"])
            elif f["type"] == "AstExprGlobal":
                nm = VALUE_BY_CALL.get(f["global"])
            elif f["type"] == "AstExprLocal":
                base = I.decl.get(lkey(f["local"]))
                if base and len(base["name"]) >= 4 and not GENERIC.match(base["name"]):
                    nm = base["name"] + "Result"
        if nm and len(nm) >= 3 and nm not in RESERVED and len(nm) <= 28:
            sug[k] = nm
    return sug


def label_arg(S, e):
    """string literal, or the literal argument of a call such as L("AutoBlock")."""
    s = str_arg(S, e)
    if s:
        return s
    if e and e["type"] == "AstExprCall" and len(e.get("args", [])) == 1:
        return str_arg(S, e["args"][0])
    return None


def callback_fn(e):
    if e["type"] == "AstExprFunction":
        return e
    if e["type"] == "AstExprCall":            # wrapper(function(v) ... end)
        for a in e.get("args", []):
            if a["type"] == "AstExprFunction":
                return a
    if e["type"] == "AstExprBinary" and e["op"] == "Or" and e["right"]["type"] == "AstExprFunction":
        return e["right"]
    return None


def stored_param(f):
    """key of the upvalue the callback writes its first parameter into (top-level stmt)."""
    if not f.get("args"):
        return None
    pk = lkey(f["args"][0])
    for st in f["body"].get("body", []):
        if st["type"] == "AstStatAssign" and len(st["vars"]) == 1 and len(st["values"]) == 1 \
                and st["vars"][0]["type"] == "AstExprLocal" and st["values"][0]["type"] == "AstExprLocal" \
                and lkey(st["values"][0]["local"]) == pk:
            return lkey(st["vars"][0]["local"])
    return None


def rename_round(S: Src):
    I = Info(S)
    sug = suggest_names(S, I)
    if not sug:
        return S.data, 0
    taken = set(I.idents) | RESERVED
    for k in I.decl:
        taken.add(I.decl[k]["name"])
    edits = []
    count = 0
    for k, nm in sug.items():
        if not nm or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", nm):
            continue
        d = I.decl[k]
        if nm == d["name"]:
            continue
        base, i = nm, 2
        while nm in taken:
            nm = "%s%d" % (base, i)
            i += 1
        taken.add(nm)
        a, z = S.span(d)
        # AstLocal location covers the name (and maybe a type annotation)
        seg = S.data[a:z]
        if not seg.startswith(d["name"].encode()):
            continue
        edits.append((a, a + len(d["name"]), nm))
        ok = True
        for r in I.reads.get(k, []):
            ra, rz = S.span(r)
            if S.data[ra:rz] != d["name"].encode():
                ok = False
                break
            edits.append((ra, rz, nm))
        # writes
        if not ok:
            return S.data, 0
        count += 1
    # collect write targets
    if count:
        names = {k: None for k in sug}

        def wv(n, parent, field):
            if n["type"] in ("AstStatAssign", "AstStatCompoundAssign"):
                tg = n["vars"] if n["type"] == "AstStatAssign" else [n["var"]]
                for x in tg:
                    if x["type"] == "AstExprLocal" and lkey(x["local"]) in names:
                        pass
            return True
        # writes are renamed via a second lookup on the edit list (same key)
        wedits = []
        newname = {}
        for (a, z, nm) in edits:
            pass
        for k in sug:
            pass
        # map decl -> new name from edits
        dmap = {}
        for k, nm in sug.items():
            d = I.decl[k]
            a, _ = S.span(d)
            for (ea, ez, en) in edits:
                if ea == a:
                    dmap[k] = en
        def wv2(n, parent, field):
            if n["type"] in ("AstStatAssign", "AstStatCompoundAssign"):
                tg = n["vars"] if n["type"] == "AstStatAssign" else [n["var"]]
                for x in tg:
                    if x["type"] == "AstExprLocal" and lkey(x["local"]) in dmap:
                        ra, rz = S.span(x)
                        wedits.append((ra, rz, dmap[lkey(x["local"])]))
            return True
        walk(S.root, wv2)
        edits += wedits
    return apply_edits(S.data, edits), count


# ------------------------------------------------------------ declarations

def merge_decls(S: Src):
    """`local x` ... `x = value` -> `local x = value` (and `local function x`).

    A local declared without a value is moved down to its first assignment when
    that assignment is a direct statement of the same block and every other use
    of the local comes after it, so scoping is unchanged.  A function value
    becomes `local function x(...)`, which is exactly `local x; x = function`.
    """
    refs = {}

    def collect(n, parent, field):
        if n["type"] == "AstExprLocal":
            refs.setdefault(lkey(n["local"]), []).append(S.span(n)[0])
        return True

    walk(S.root, collect)
    edits = []
    count = 0

    def blocks(n, parent, field):
        nonlocal count
        if n["type"] != "AstStatBlock":
            return True
        body = n.get("body", [])
        for i, st in enumerate(body):
            if st["type"] != "AstStatLocal" or st.get("values"):
                continue
            if any(v.get("luauType") or v.get("annotation") for v in st["vars"]):
                continue
            moved = set()
            for v in st["vars"]:
                k = lkey(v)
                name = v["name"]
                rs = refs.get(k, [])
                if not rs:
                    continue
                first = min(rs)
                for st2 in body[i + 1:]:
                    a2, z2 = S.span(st2)
                    if a2 > first:
                        break
                    if z2 < first:
                        continue
                    # st2 contains the first use: must be `x = value`
                    if st2["type"] != "AstStatAssign" or len(st2["vars"]) != 1 or len(st2["values"]) != 1:
                        break
                    tg = st2["vars"][0]
                    if tg["type"] != "AstExprLocal" or lkey(tg["local"]) != k or S.span(tg)[0] != first:
                        break
                    val = st2["values"][0]
                    va, vz = S.span(val)
                    inside = [r for r in rs if va <= r < vz]
                    vtxt = S.data[va:vz]
                    if val["type"] == "AstExprFunction" and vtxt.startswith(b"function") \
                            and S.data[va + 8:va + 9] == b"(":
                        edits.append((a2, va + 8, "local function " + name))
                    elif not inside and not re.search(rb"\b" + re.escape(name.encode()) + rb"\b", vtxt):
                        edits.append((a2, va, "local " + name + " = "))
                    else:
                        break
                    moved.add(k)
                    count += 1
                    break
            if moved:
                a, z = S.span(st)
                keep = [v["name"] for v in st["vars"] if lkey(v) not in moved]
                if keep:
                    ind = S.data[S.ls[bisect.bisect_right(S.ls, a) - 1]:a].decode("latin1")
                    lines = [", ".join(keep[j:j + 10]) for j in range(0, len(keep), 10)]
                    edits.append((a, z, ("\n" + ind).join("local " + l for l in lines)))
                else:
                    # drop the whole line
                    ls = S.ls[bisect.bisect_right(S.ls, a) - 1]
                    le = S.data.find(b"\n", z)
                    le = len(S.data) if le < 0 else le + 1
                    if S.data[ls:a].strip() == b"" and S.data[z:le].strip() == b"":
                        edits.append((ls, le, ""))
                    else:
                        edits.append((a, z, ""))
        return True

    walk(S.root, blocks)
    if not edits:
        return S.data, 0
    return apply_edits(S.data, edits), count


# ----------------------------------------------------------------- report

URL_RE = re.compile(rb"https?://[^\s\"'<>]+")


def strings_report(S: Src):
    found = []

    def visit(n, parent, field):
        if n["type"] == "AstExprConstantString":
            b = decode_lua_string(S.text(n))
            if b is not None:
                line = S.off(n["location"].split(" - ")[0])
                found.append((bisect.bisect_right(S.ls, line), b))
        return True

    walk(S.root, visit)
    urls, hooks, b64, all_s = [], [], [], []
    seen = set()
    for ln, b in found:
        if b in seen:
            continue
        seen.add(b)
        try:
            s = b.decode("utf-8")
            if any(ord(c) < 32 for c in s):
                s = render_string(b)[1:-1]
        except UnicodeDecodeError:
            s = render_string(b)[1:-1]
        all_s.append((ln, s))
        for u in URL_RE.findall(b):
            u = u.decode("latin1")
            (hooks if re.search(r"discord(app)?\.com/api/webhooks|webhook", u, re.I) else urls).append((ln, u))
        if len(b) >= 16 and re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", b) and len(b) % 4 == 0:
            try:
                d = base64.b64decode(b, validate=True)
                txt = d.decode("utf-8")
                if sum(c.isprintable() for c in txt) > 0.9 * len(txt):
                    b64.append((ln, s, txt))
            except Exception:
                pass
        elif len(b) >= 16 and len(b) % 2 == 0 and re.fullmatch(rb"[0-9a-fA-F]+", b):
            try:
                txt = bytes.fromhex(b.decode()).decode("utf-8")
                if all(c.isprintable() or c in "\n\t" for c in txt):
                    b64.append((ln, s, "(hex) " + txt))
            except Exception:
                pass
    lines = ["# strings report (polish.py)", ""]
    if hooks:
        lines.append("## WEBHOOKS (possible data exfiltration)")
        lines += ["  line %d: %s" % x for x in hooks]
        lines.append("")
    if urls:
        lines.append("## URLs")
        lines += ["  line %d: %s" % x for x in urls]
        lines.append("")
    if b64:
        lines.append("## encoded strings (decoded)")
        lines += ["  line %d: %s\n      -> %s" % x for x in b64]
        lines.append("")
    lines.append("## all strings (%d unique)" % len(all_s))
    lines += ["  line %d: %s" % (ln, s if len(s) < 300 else s[:300] + "...") for ln, s in all_s]
    return "\n".join(lines) + "\n", hooks, urls, b64


# ------------------------------------------------------------------- main


def run_luaclean(ast_exe, data, tmp):
    inp, out = tmp + ".lc_in.lua", tmp + ".lc_out.lua"
    with open(inp, "wb") as f:
        f.write(data)
    r = subprocess.run([sys.executable, os.path.join(HERE, "luaclean.py"), ast_exe, inp, out],
                       capture_output=True)
    try:
        if r.returncode == 0 and os.path.exists(out):
            return open(out, "rb").read()
        return data
    finally:
        for p in (inp, out):
            try:
                os.unlink(p)
            except OSError:
                pass


def tidy_blank(cur: bytes) -> bytes:
    """Blank lines left where statements were removed: never two in a row, none
    at the start of the file, right after a block opener or right before `end`."""
    lines = cur.split(b"\n")
    out = []
    for i, l in enumerate(lines):
        if l.strip() == b"":
            if not out or out[-1].strip() == b"":
                continue
            prev = out[-1].rstrip()
            if re.search(rb"(\bdo|\bthen|\belse|\brepeat|\)|\{)$", prev) and \
                    re.match(rb"\s*(local\s+)?(function\b|.*\bfunction\s*\(|.*\bdo$|.*\bthen$|.*\{$|.*\($|repeat$)", prev):
                continue
            nxt = next((x for x in lines[i + 1:] if x.strip()), b"")
            if re.match(rb"\s*(end\b|else\b|elseif\b|until\b|\}|\))", nxt):
                continue
            out.append(b"")
        else:
            out.append(l.rstrip(b" \t") if l.strip() else l)
    while out and out[-1] == b"":
        out.pop()
    return b"\n".join(out) + b"\n"


def polish(ast_exe, data, tmp, rename=True, clean=True, log=sys.stderr):
    total = {"folded": 0, "strings": 0, "branches": 0, "renamed": 0}
    cur = data
    for rnd in range(12):
        try:
            S = Src(cur, ast_exe, tmp)
        except SyntaxError:
            break
        nxt, st = fold_round(S, propagate=True, strings=(rnd == 0) or True)
        changed = nxt != cur
        if changed and parse_ok(ast_exe, nxt, tmp):
            for k in st:
                total[k] += st[k]
            cur = nxt
        elif changed:
            # retry without propagation (most aggressive part)
            nxt2, st = fold_round(S, propagate=False)
            if nxt2 != cur and parse_ok(ast_exe, nxt2, tmp):
                for k in st:
                    total[k] += st[k]
                cur = nxt2
                changed = True
            else:
                changed = False
        if clean:
            c2 = run_luaclean(ast_exe, cur, tmp)
            if c2 != cur and parse_ok(ast_exe, c2, tmp):
                cur = c2
                changed = True
        if not changed:
            break
    if rename:
        for _ in range(3):
            try:
                S = Src(cur, ast_exe, tmp)
            except SyntaxError:
                break
            nxt, n = rename_round(S)
            if not n or nxt == cur or not parse_ok(ast_exe, nxt, tmp):
                break
            total["renamed"] += n
            cur = nxt
    # move `local x` declarations down to their first assignment
    total["merged"] = 0
    for _ in range(3):
        try:
            S = Src(cur, ast_exe, tmp)
        except SyntaxError:
            break
        nxt, n = merge_decls(S)
        if not n or nxt == cur or not parse_ok(ast_exe, nxt, tmp):
            break
        total["merged"] += n
        cur = nxt
    # tidy blank-line runs left behind by removals
    cur = tidy_blank(cur)
    log.write("[*] polish: %(folded)d expressions folded, %(strings)d strings decoded, "
              "%(branches)d constant branches removed, %(renamed)d locals renamed, "
              "%(merged)d declarations merged\n" % total)
    return cur


def main():
    a = sys.argv[1:]
    if len(a) < 3:
        sys.exit(__doc__)
    ast_exe, src, dst = a[:3]
    report = None
    if "--report" in a:
        report = a[a.index("--report") + 1]
    data = open(src, "rb").read()
    tmp = dst + ".polish_tmp.lua"
    try:
        out = polish(ast_exe, data, tmp, rename="--no-rename" not in a, clean="--no-clean" not in a)
        if not parse_ok(ast_exe, out, tmp):
            sys.stderr.write("[!] polish: result did not parse, keeping input\n")
            out = data
        if report:
            try:
                S = Src(out, ast_exe, tmp)
                txt, hooks, urls, b64 = strings_report(S)
                with open(report, "w", encoding="utf-8") as f:
                    f.write(txt)
                if hooks:
                    sys.stderr.write("[!] %d webhook URL(s) found -- see %s\n" % (len(hooks), report))
            except Exception as e:  # report is best effort
                sys.stderr.write("[!] strings report failed: %s\n" % e)
        with open(dst, "wb") as f:
            f.write(out)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    main()
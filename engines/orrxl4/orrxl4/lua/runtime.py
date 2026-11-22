import math


class LuaError(Exception):
    def __init__(self, value):
        Exception.__init__(self, value)
        self.value = value


class SandboxLimit(Exception):
    pass


class LuaTable:
    __slots__ = ("d", "n", "meta")

    def __init__(self):
        self.d = {}
        self.n = 0
        self.meta = None

    def set(self, k, v):
        c = k.__class__
        if c is float:
            if k.is_integer():
                k = int(k)
                c = int
            elif k != k:
                raise LuaError(b"table index is NaN")
        elif k is None:
            raise LuaError(b"table index is nil")
        d = self.d
        if v is None:
            if k in d:
                del d[k]
                if c is int and k == self.n:
                    n = k - 1
                    while n > 0 and n not in d:
                        n -= 1
                    self.n = n
        else:
            d[k] = v
            if c is int and k == self.n + 1:
                n = k
                while (n + 1) in d:
                    n += 1
                self.n = n

    def get(self, k):
        if k.__class__ is float and k.is_integer():
            k = int(k)
        return self.d.get(k)

    def items(self):
        n = self.n
        d = self.d
        out = [(i, d[i]) for i in range(1, n + 1) if i in d]
        for k, v in d.items():
            if not (k.__class__ is int and 1 <= k <= n):
                out.append((k, v))
        return out


class Proto:
    __slots__ = ("nslots", "nparams", "vararg", "body", "upinfo", "name")


class LuaFunction:
    __slots__ = ("p", "up")

    def __init__(self, p, up):
        self.p = p
        self.up = up

    def __call__(self, *args):
        p = self.p
        fr = [None] * p.nslots
        fr[0] = self.up
        n = p.nparams
        na = len(args)
        for i in range(n):
            fr[2 + i] = [args[i] if i < na else None]
        if p.vararg:
            fr[1] = list(args[n:])
        r = p.body(fr)
        if r.__class__ is list:
            return r
        return []


def type_name(v):
    if v is None:
        return "nil"
    c = v.__class__
    if c is bool:
        return "boolean"
    if c is int or c is float:
        return "number"
    if c is bytes:
        return "string"
    if c is LuaTable:
        return "table"
    if callable(v):
        return "function"
    return "userdata"


def fmt_number(v):
    if v.__class__ is int:
        return str(v)
    if v != v:
        return "nan"
    if v in (math.inf, -math.inf):
        return "inf" if v > 0 else "-inf"
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return "%.14g" % v


def tostr(v):
    c = v.__class__
    if c is bytes:
        return v
    if v is None:
        return b"nil"
    if c is bool:
        return b"true" if v else b"false"
    if c is int or c is float:
        return fmt_number(v).encode()
    if c is LuaTable:
        if v.meta is not None:
            h = v.meta.d.get(b"__tostring")
            if h is not None:
                r = call_value(h, [v])
                return tostr(r[0]) if r else b"nil"
        return ("table: 0x%08x" % (id(v) & 0xFFFFFFFF)).encode()
    return ("function: 0x%08x" % (id(v) & 0xFFFFFFFF)).encode()


def str_to_number(b):
    s = b.strip().decode("latin-1")
    if not s:
        return None
    try:
        low = s.lower()
        if low.startswith(("0x", "-0x")):
            return int(low, 16)
        if low in ("inf", "-inf", "nan", "infinity", "-infinity", "+inf", "+infinity"):
            return None
        if low.lstrip("+-").isdigit():
            return int(low)
        return float(low)
    except ValueError:
        return None


def to_number(v):
    c = v.__class__
    if c is int or c is float:
        return v
    if c is bytes:
        return str_to_number(v)
    return None


def truthy(v):
    return v is not None and v is not False


def lua_eq(a, b):
    if a is b:
        return True
    ca = a.__class__
    cb = b.__class__
    if ca is bool or cb is bool:
        return False
    if ca is LuaTable and cb is LuaTable and a.meta is not None and a.meta is b.meta:
        h = a.meta.d.get(b"__eq")
        if h is not None:
            r = call_value(h, [a, b])
            return bool(r) and truthy(r[0])
    return a == b


def call_value(f, args):
    if callable(f):
        return f(*args)
    if f.__class__ is LuaTable and f.meta is not None:
        h = f.meta.d.get(b"__call")
        if h is not None:
            return call_value(h, [f] + list(args))
    raise LuaError(("attempt to call a %s value" % type_name(f)).encode())


STRING_META = LuaTable()


def index(o, k):
    if o.__class__ is LuaTable:
        if k.__class__ is float and k.is_integer():
            k = int(k)
        v = o.d.get(k)
        if v is None and o.meta is not None:
            h = o.meta.d.get(b"__index")
            if h is not None:
                if h.__class__ is LuaTable:
                    return index(h, k)
                r = call_value(h, [o, k])
                return r[0] if r else None
        return v
    if o.__class__ is bytes:
        return STRING_META.d.get(k)
    raise LuaError(("attempt to index a %s value" % type_name(o)).encode())


def setindex(o, k, v):
    if o.__class__ is LuaTable:
        if o.meta is not None:
            h = o.meta.d.get(b"__newindex")
            if h is not None and o.get(k) is None:
                if h.__class__ is LuaTable:
                    setindex(h, k, v)
                else:
                    call_value(h, [o, k, v])
                return
        o.set(k, v)
        return
    raise LuaError(("attempt to index a %s value" % type_name(o)).encode())


def length(v):
    if v.__class__ is bytes:
        return len(v)
    if v.__class__ is LuaTable:
        if v.meta is not None:
            h = v.meta.d.get(b"__len")
            if h is not None:
                r = call_value(h, [v])
                return r[0] if r else None
        return v.n
    raise LuaError(("attempt to get length of a %s value" % type_name(v)).encode())


def concat(a, b):
    ca = a.__class__
    cb = b.__class__
    if ca is bytes and cb is bytes:
        return a + b
    if (ca is bytes or ca is int or ca is float) and (cb is bytes or cb is int or cb is float):
        return tostr(a) + tostr(b)
    for x in (a, b):
        if x.__class__ is LuaTable and x.meta is not None:
            h = x.meta.d.get(b"__concat")
            if h is not None:
                r = call_value(h, [a, b])
                return r[0] if r else None
    bad = a if not (ca is bytes or ca is int or ca is float) else b
    raise LuaError(("attempt to concatenate a %s value" % type_name(bad)).encode())


def _mod(a, b):
    try:
        return a % b
    except ZeroDivisionError:
        return math.nan


def _div(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0 or a != a:
            return math.nan
        neg = (a < 0) != (math.copysign(1.0, b) < 0)
        return -math.inf if neg else math.inf


def _idiv(a, b):
    try:
        r = a // b
        return r
    except ZeroDivisionError:
        return _div(a, b)


def _pow(a, b):
    try:
        return float(a) ** float(b)
    except OverflowError:
        return math.inf
    except ZeroDivisionError:
        return math.inf
    except ValueError:
        return math.nan


def _toint(a):
    if a.__class__ is float:
        if not a.is_integer():
            raise LuaError(b"number has no integer representation")
        return int(a)
    return a


def _shl(a, b):
    a = _toint(a) & 0xFFFFFFFFFFFFFFFF
    b = _toint(b)
    if b < 0:
        return a >> -b
    return (a << b) & 0xFFFFFFFFFFFFFFFF if b < 64 else 0


ARITH = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "/": _div,
    "%": _mod,
    "//": _idiv,
    "^": _pow,
    "&": lambda a, b: _toint(a) & _toint(b),
    "|": lambda a, b: _toint(a) | _toint(b),
    "~": lambda a, b: _toint(a) ^ _toint(b),
    "<<": _shl,
    ">>": lambda a, b: _shl(a, -_toint(b)),
}
META_NAMES = {
    "+": b"__add", "-": b"__sub", "*": b"__mul", "/": b"__div", "%": b"__mod",
    "//": b"__idiv", "^": b"__pow",
}


def make_arith(op):
    fn = ARITH[op]
    meta = META_NAMES.get(op)

    def run(a, b):
        ca = a.__class__
        cb = b.__class__
        if (ca is int or ca is float) and (cb is int or cb is float):
            return fn(a, b)
        na = to_number(a)
        nb = to_number(b)
        if na is None or nb is None:
            if meta is not None:
                for x in (a, b):
                    if x.__class__ is LuaTable and x.meta is not None:
                        h = x.meta.d.get(meta)
                        if h is not None:
                            r = call_value(h, [a, b])
                            return r[0] if r else None
            bad = a if na is None else b
            raise LuaError(("attempt to perform arithmetic on a %s value" % type_name(bad)).encode())
        return fn(na, nb)

    return run


def lt(a, b):
    ca = a.__class__
    cb = b.__class__
    if (ca is int or ca is float) and (cb is int or cb is float):
        return a < b
    if ca is bytes and cb is bytes:
        return a < b
    for x in (a, b):
        if x.__class__ is LuaTable and x.meta is not None:
            h = x.meta.d.get(b"__lt")
            if h is not None:
                r = call_value(h, [a, b])
                return bool(r) and truthy(r[0])
    raise LuaError(("attempt to compare %s with %s" % (type_name(a), type_name(b))).encode())


def le(a, b):
    ca = a.__class__
    cb = b.__class__
    if (ca is int or ca is float) and (cb is int or cb is float):
        return a <= b
    if ca is bytes and cb is bytes:
        return a <= b
    for x in (a, b):
        if x.__class__ is LuaTable and x.meta is not None:
            h = x.meta.d.get(b"__le")
            if h is not None:
                r = call_value(h, [a, b])
                return bool(r) and truthy(r[0])
    raise LuaError(("attempt to compare %s with %s" % (type_name(a), type_name(b))).encode())

import time

from .parser import Node
from . import runtime as R
from .runtime import LuaTable, LuaFunction, LuaError, SandboxLimit, Proto

BREAK = object()
MULTI = ("call", "method", "vararg")


class FuncState:
    def __init__(self, parent):
        self.parent = parent
        self.blocks = [{}]
        self.nslots = 2
        self.upvals = []
        self.upmap = {}

    def declare(self, name):
        slot = self.nslots
        self.nslots += 1
        self.blocks[-1][name] = slot
        return slot


class Limits:
    def __init__(self, timeout):
        self.count = 0
        self.deadline = time.monotonic() + timeout if timeout else None

    def tick(self):
        self.count += 1
        if self.count & 0x1FFF == 0 and self.deadline is not None and time.monotonic() > self.deadline:
            raise SandboxLimit("execution time limit reached")


class Compiler:
    def __init__(self, genv, limits, fast=None):
        self.genv = genv
        self.limits = limits
        self.fast = fast
        self.arith = {op: R.make_arith(op) for op in R.ARITH}

    def compile_chunk(self, tree):
        fs = FuncState(None)
        body = self.block_in(tree.body, fs)
        p = Proto()
        p.nslots = fs.nslots
        p.nparams = 0
        p.vararg = True
        p.body = body
        p.upinfo = []
        p.name = "chunk"
        return LuaFunction(p, [])

    def resolve(self, name, fs):
        for blk in reversed(fs.blocks):
            if name in blk:
                return ("local", blk[name])
        idx = self.find_up(name, fs)
        if idx is not None:
            return ("up", idx)
        return ("global", None)

    def find_up(self, name, fs):
        if name in fs.upmap:
            return fs.upmap[name]
        p = fs.parent
        if p is None:
            return None
        for blk in reversed(p.blocks):
            if name in blk:
                fs.upvals.append((True, blk[name]))
                fs.upmap[name] = len(fs.upvals) - 1
                return fs.upmap[name]
        pi = self.find_up(name, p)
        if pi is None:
            return None
        fs.upvals.append((False, pi))
        fs.upmap[name] = len(fs.upvals) - 1
        return fs.upmap[name]

    def block_in(self, stmts, fs):
        fs.blocks.append({})
        compiled = [self.stmt(s, fs) for s in stmts]
        fs.blocks.pop()
        return self.seq(compiled)

    def seq(self, compiled):
        if not compiled:
            return lambda fr: None
        if len(compiled) == 1:
            return compiled[0]

        def run(fr):
            for s in compiled:
                r = s(fr)
                if r is not None:
                    return r
        return run

    def stmt(self, n, fs):
        return getattr(self, "s_" + n.t)(n, fs)

    def s_local(self, n, fs):
        vals = self.explist(n.exprs, fs)
        slots = [fs.declare(x.name) for x in n.names]
        if len(slots) == 1:
            s0 = slots[0]

            def run1(fr):
                v = vals(fr)
                fr[s0] = [v[0] if v else None]
            return run1

        def run(fr):
            v = vals(fr)
            nv = len(v)
            for i, s in enumerate(slots):
                fr[s] = [v[i] if i < nv else None]
        return run

    def s_localfunc(self, n, fs):
        slot = fs.declare(n.name.name)
        mk = self.func(n.func, fs)

        def run(fr):
            cell = [None]
            fr[slot] = cell
            cell[0] = mk(fr)
        return run

    def s_funcstat(self, n, fs):
        mk = self.func(n.func, fs)
        target = n.target
        if n.method is not None:
            obj = self.ex(target, fs)
            key = n.method.encode("latin-1")

            def run(fr):
                R.setindex(obj(fr), key, mk(fr))
            return run
        setter = self.setter(target, fs)

        def run2(fr):
            setter(fr, mk(fr))
        return run2

    def setter(self, t, fs):
        if t.t == "name":
            kind, i = self.resolve(t.name, fs)
            if kind == "local":
                def set_local(fr, v):
                    fr[i][0] = v
                return set_local
            if kind == "up":
                def set_up(fr, v):
                    fr[0][i][0] = v
                return set_up
            key = t.name.encode("latin-1")
            g = self.genv

            def set_global(fr, v):
                g.set(key, v)
            return set_global
        o = self.ex(t.obj, fs)
        k = self.ex(t.key, fs)

        def set_index(fr, v):
            R.setindex(o(fr), k(fr), v)
        return set_index

    def s_assign(self, n, fs):
        vals = self.explist(n.exprs, fs)
        if len(n.targets) == 1:
            t = n.targets[0]
            if t.t == "index":
                o = self.ex(t.obj, fs)
                k = self.ex(t.key, fs)

                def run_idx(fr):
                    v = vals(fr)
                    R.setindex(o(fr), k(fr), v[0] if v else None)
                return run_idx
            st = self.setter(t, fs)

            def run1(fr):
                v = vals(fr)
                st(fr, v[0] if v else None)
            return run1
        parts = []
        for t in n.targets:
            if t.t == "index":
                parts.append((self.ex(t.obj, fs), self.ex(t.key, fs), None))
            else:
                parts.append((None, None, self.setter(t, fs)))

        def run(fr):
            pre = []
            for o, k, st in parts:
                pre.append((o(fr), k(fr)) if o is not None else None)
            v = vals(fr)
            nv = len(v)
            for i, (o, k, st) in enumerate(parts):
                val = v[i] if i < nv else None
                if o is not None:
                    R.setindex(pre[i][0], pre[i][1], val)
                else:
                    st(fr, val)
        return run

    def s_callstat(self, n, fs):
        m = self.multi(n.call, fs)

        def run(fr):
            m(fr)
        return run

    def s_do(self, n, fs):
        return self.block_in(n.body, fs)

    def s_break(self, n, fs):
        return lambda fr: BREAK

    def s_return(self, n, fs):
        vals = self.explist(n.exprs, fs)
        return lambda fr: vals(fr)

    def s_if(self, n, fs):
        clauses = [(self.ex(c, fs), self.block_in(b, fs)) for c, b in n.clauses]
        orelse = self.block_in(n.orelse, fs) if n.orelse is not None else None

        def run(fr):
            for c, b in clauses:
                v = c(fr)
                if v is not None and v is not False:
                    return b(fr)
            if orelse is not None:
                return orelse(fr)
        return run

    def s_while(self, n, fs):
        cond = self.ex(n.cond, fs)
        body = self.block_in(n.body, fs)
        tick = self.limits.tick

        def run(fr):
            while True:
                v = cond(fr)
                if v is None or v is False:
                    return None
                r = body(fr)
                if r is not None:
                    if r is BREAK:
                        return None
                    return r
                tick()
        return run

    def s_repeat(self, n, fs):
        fs.blocks.append({})
        compiled = [self.stmt(s, fs) for s in n.body]
        cond = self.ex(n.cond, fs)
        fs.blocks.pop()
        body = self.seq(compiled)
        tick = self.limits.tick

        def run(fr):
            while True:
                r = body(fr)
                if r is not None:
                    if r is BREAK:
                        return None
                    return r
                v = cond(fr)
                if v is not None and v is not False:
                    return None
                tick()
        return run

    def s_fornum(self, n, fs):
        start = self.ex(n.start, fs)
        stop = self.ex(n.stop, fs)
        step = self.ex(n.step, fs) if n.step is not None else None
        fs.blocks.append({})
        slot = fs.declare(n.var.name)
        body = self.block_in(n.body, fs)
        fs.blocks.pop()
        tick = self.limits.tick

        def run(fr):
            a = R.to_number(start(fr))
            b = R.to_number(stop(fr))
            st = R.to_number(step(fr)) if step is not None else 1
            if a is None or b is None or st is None:
                raise LuaError(b"'for' initial value must be a number")
            if st == 0:
                raise LuaError(b"'for' step is zero")
            i = a
            while (i <= b) if st > 0 else (i >= b):
                fr[slot] = [i]
                r = body(fr)
                if r is not None:
                    if r is BREAK:
                        return None
                    return r
                i += st
                tick()
        return run

    def s_forin(self, n, fs):
        vals = self.explist(n.exprs, fs)
        fs.blocks.append({})
        slots = [fs.declare(x.name) for x in n.names]
        body = self.block_in(n.body, fs)
        fs.blocks.pop()
        tick = self.limits.tick
        nv = len(slots)

        def assign(fr, k, v):
            fr[slots[0]] = [k]
            if nv > 1:
                fr[slots[1]] = [v]
                for s in slots[2:]:
                    fr[s] = [None]

        def run(fr):
            v = vals(fr)
            f = v[0] if v else None
            s = v[1] if len(v) > 1 else None
            c = v[2] if len(v) > 2 else None
            if f is NEXT and s.__class__ is LuaTable and c is None:
                for k, val in s.items():
                    assign(fr, k, val)
                    r = body(fr)
                    if r is not None:
                        if r is BREAK:
                            return None
                        return r
                    tick()
                return None
            while True:
                res = R.call_value(f, [s, c])
                k = res[0] if res else None
                if k is None:
                    return None
                c = k
                fr[slots[0]] = [k]
                for i in range(1, nv):
                    fr[slots[i]] = [res[i] if i < len(res) else None]
                r = body(fr)
                if r is not None:
                    if r is BREAK:
                        return None
                    return r
                tick()
        return run

    def func(self, n, fs):
        sub = FuncState(fs)
        for p in n.params:
            sub.declare(p.name)
        p = Proto()
        p.nparams = len(n.params)
        p.vararg = n.vararg
        p.name = n.name
        body = self.block_in(n.body, sub)
        p.body = body
        p.nslots = sub.nslots
        p.upinfo = list(sub.upvals)
        upinfo = p.upinfo

        def mk(fr):
            up = [fr[i] if is_local else fr[0][i] for is_local, i in upinfo]
            return LuaFunction(p, up)
        if self.fast is not None:
            return self.fast.wrap(n, mk)
        return mk

    def explist(self, nodes, fs):
        if not nodes:
            return lambda fr: []
        last = nodes[-1]
        if last.t in MULTI:
            heads = [self.ex(x, fs) for x in nodes[:-1]]
            tail = self.multi(last, fs)
            if not heads:
                return tail

            def run_m(fr):
                out = [h(fr) for h in heads]
                out.extend(tail(fr))
                return out
            return run_m
        fns = [self.ex(x, fs) for x in nodes]
        if len(fns) == 1:
            f0 = fns[0]
            return lambda fr: [f0(fr)]
        if len(fns) == 2:
            f0, f1 = fns
            return lambda fr: [f0(fr), f1(fr)]
        return lambda fr: [f(fr) for f in fns]

    def multi(self, n, fs):
        if n.t == "call":
            f = self.ex(n.fn, fs)
            args = self.explist(n.args, fs)

            def run(fr):
                fn = f(fr)
                a = args(fr)
                if callable(fn):
                    return fn(*a)
                return R.call_value(fn, a)
            return run
        if n.t == "method":
            o = self.ex(n.obj, fs)
            key = n.name.encode("latin-1")
            args = self.explist(n.args, fs)

            def run_m(fr):
                obj = o(fr)
                fn = R.index(obj, key)
                a = args(fr)
                if callable(fn):
                    return fn(obj, *a)
                return R.call_value(fn, [obj] + a)
            return run_m
        if n.t == "vararg":
            return lambda fr: list(fr[1]) if fr[1] else []
        f = self.ex(n, fs)
        return lambda fr: [f(fr)]

    def ex(self, n, fs):
        return getattr(self, "x_" + n.t)(n, fs)

    def x_nil(self, n, fs):
        return lambda fr: None

    def x_bool(self, n, fs):
        v = n.v
        return lambda fr: v

    def x_num(self, n, fs):
        v = n.v
        return lambda fr: v

    def x_str(self, n, fs):
        v = n.v
        return lambda fr: v

    def x_vararg(self, n, fs):
        return lambda fr: fr[1][0] if fr[1] else None

    def x_paren(self, n, fs):
        return self.ex(n.e, fs)

    def x_func(self, n, fs):
        return self.func(n, fs)

    def x_name(self, n, fs):
        kind, i = self.resolve(n.name, fs)
        if kind == "local":
            return lambda fr: fr[i][0]
        if kind == "up":
            return lambda fr: fr[0][i][0]
        key = n.name.encode("latin-1")
        d = self.genv.d
        return lambda fr: d.get(key)

    def x_index(self, n, fs):
        o = self.ex(n.obj, fs)
        if n.key.t == "str":
            key = n.key.v

            def run_c(fr):
                obj = o(fr)
                if obj.__class__ is LuaTable and obj.meta is None:
                    return obj.d.get(key)
                return R.index(obj, key)
            return run_c
        k = self.ex(n.key, fs)
        return lambda fr: R.index(o(fr), k(fr))

    def x_call(self, n, fs):
        m = self.multi(n, fs)

        def run(fr):
            r = m(fr)
            return r[0] if r else None
        return run

    x_method = x_call

    def x_table(self, n, fs):
        items = n.items
        if items and all(k == "pos" and v.t == "num" for k, _, v in items):
            const = {i + 1: v.v for i, (_, _, v) in enumerate(items)}
            size = len(items)

            def run_const(fr):
                t = LuaTable()
                t.d = dict(const)
                t.n = size
                return t
            return run_const
        compiled = []
        for idx, (kind, key, val) in enumerate(items):
            is_last = idx == len(items) - 1
            if kind == "pos":
                if is_last and val.t in MULTI:
                    compiled.append(("multi", None, self.multi(val, fs)))
                else:
                    compiled.append(("pos", None, self.ex(val, fs)))
            elif kind == "name":
                compiled.append(("key", key.encode("latin-1"), self.ex(val, fs)))
            else:
                compiled.append(("kexpr", self.ex(key, fs), self.ex(val, fs)))

        def run(fr):
            t = LuaTable()
            pos = 1
            for kind, k, v in compiled:
                if kind == "pos":
                    t.set(pos, v(fr))
                    pos += 1
                elif kind == "multi":
                    for x in v(fr):
                        t.set(pos, x)
                        pos += 1
                elif kind == "key":
                    t.set(k, v(fr))
                else:
                    t.set(k(fr), v(fr))
            return t
        return run

    def x_unop(self, n, fs):
        e = self.ex(n.e, fs)
        op = n.op
        if op == "not":
            return lambda fr: not R.truthy(e(fr))
        if op == "#":
            return lambda fr: R.length(e(fr))
        if op == "-":
            def neg(fr):
                v = e(fr)
                c = v.__class__
                if c is int or c is float:
                    return -v
                nv = R.to_number(v)
                if nv is None:
                    if c is LuaTable and v.meta is not None:
                        h = v.meta.d.get(b"__unm")
                        if h is not None:
                            r = R.call_value(h, [v, v])
                            return r[0] if r else None
                    raise LuaError(("attempt to perform arithmetic on a %s value" % R.type_name(v)).encode())
                return -nv
            return neg

        def bnot(fr):
            return ~R._toint(R.to_number(e(fr))) & 0xFFFFFFFFFFFFFFFF
        return bnot

    def x_binop(self, n, fs):
        op = n.op
        l = self.ex(n.l, fs)
        r = self.ex(n.r, fs)
        if op == "and":
            def run_and(fr):
                a = l(fr)
                if a is None or a is False:
                    return a
                return r(fr)
            return run_and
        if op == "or":
            def run_or(fr):
                a = l(fr)
                if a is not None and a is not False:
                    return a
                return r(fr)
            return run_or
        if op == "==":
            return lambda fr: R.lua_eq(l(fr), r(fr))
        if op == "~=":
            return lambda fr: not R.lua_eq(l(fr), r(fr))
        if op == "<":
            return lambda fr: R.lt(l(fr), r(fr))
        if op == "<=":
            return lambda fr: R.le(l(fr), r(fr))
        if op == ">":
            return lambda fr: R.lt(r(fr), l(fr))
        if op == ">=":
            return lambda fr: R.le(r(fr), l(fr))
        if op == "..":
            return lambda fr: R.concat(l(fr), r(fr))
        fn = self.arith[op]
        if op == "+":
            def add(fr):
                a = l(fr)
                b = r(fr)
                if a.__class__ is int and b.__class__ is int:
                    return a + b
                return fn(a, b)
            return add
        if op == "%":
            def mod(fr):
                a = l(fr)
                b = r(fr)
                if a.__class__ is int and b.__class__ is int and b:
                    return a % b
                return fn(a, b)
            return mod
        return lambda fr: fn(l(fr), r(fr))


def NEXT(t, k=None):
    items = t.items()
    if k is None:
        return list(items[0]) if items else [None]
    for i, (key, _) in enumerate(items):
        if R.lua_eq(key, k):
            if i + 1 < len(items):
                return list(items[i + 1])
            return [None]
    raise LuaError(b"invalid key to 'next'")

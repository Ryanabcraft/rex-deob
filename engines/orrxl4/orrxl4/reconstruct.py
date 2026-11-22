import re

from .lua import parse, ParseError, LexError, strip_comments
from .lua.lexer import KEYWORDS

OBF_RE = re.compile(r"^_[IlO01]{4,}$|^_[A-Za-z0-9]{10,}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PURE_LIBS = {
    "math": {"floor", "ceil", "max", "min", "abs", "sqrt"},
    "string": {"len", "byte", "sub", "char", "rep", "upper", "lower", "reverse"},
    "table": set(),
}
PREFIX = {"fn": "func", "param": "arg", "var": "var", "loop": "i"}


class Binding:
    __slots__ = ("name", "new", "uses", "alias")

    def __init__(self, name, new):
        self.name = name
        self.new = new
        self.uses = 0
        self.alias = None


class Resolver:
    def __init__(self, rename, taken):
        self.rename = rename
        self.taken = taken
        self.scopes = [{}]
        self.counters = {}
        self.fields = {}

    def fresh(self, old, kind):
        if not (self.rename and OBF_RE.match(old)):
            return old
        prefix = PREFIX[kind]
        while True:
            c = self.counters.get(prefix, 0) + 1
            self.counters[prefix] = c
            cand = "%s_%d" % (prefix, c)
            if cand not in self.taken:
                self.taken.add(cand)
                return cand

    def field(self, old):
        if not (self.rename and OBF_RE.match(old)):
            return old
        if old not in self.fields:
            self.fields[old] = self.fresh_field()
        return self.fields[old]

    def fresh_field(self):
        while True:
            c = self.counters.get("field", 0) + 1
            self.counters["field"] = c
            cand = "field_%d" % c
            if cand not in self.taken:
                self.taken.add(cand)
                return cand

    def declare(self, node, kind):
        b = Binding(node.name, self.fresh(node.name, kind))
        self.scopes[-1][node.name] = b
        node.name = b.new
        node.binding = b
        return b

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return None

    def push(self):
        self.scopes.append({})

    def pop(self):
        self.scopes.pop()

    def block(self, stmts, scope=True):
        if scope:
            self.push()
        for s in stmts:
            self.stmt(s)
        if scope:
            self.pop()

    def stmt(self, s):
        t = s.t
        if t == "local":
            for e in s.exprs:
                self.expr(e)
            for n in s.names:
                self.declare(n, "var")
            if len(s.names) == 1 and len(s.exprs) == 1:
                e = s.exprs[0]
                if e.t == "name" and e.binding is None and e.name in PURE_LIBS:
                    s.names[0].binding.alias = e.name
        elif t == "localfunc":
            self.declare(s.name, "fn")
            self.expr(s.func)
        elif t == "funcstat":
            self.expr(s.target)
            self.expr(s.func)
        elif t == "assign":
            for e in s.exprs:
                self.expr(e)
            for tg in s.targets:
                self.expr(tg)
        elif t == "callstat":
            self.expr(s.call)
        elif t == "do":
            self.block(s.body)
        elif t == "while":
            self.expr(s.cond)
            self.block(s.body)
        elif t == "repeat":
            self.push()
            for x in s.body:
                self.stmt(x)
            self.expr(s.cond)
            self.pop()
        elif t == "if":
            for c, b in s.clauses:
                self.expr(c)
                self.block(b)
            if s.orelse is not None:
                self.block(s.orelse)
        elif t == "fornum":
            self.expr(s.start)
            self.expr(s.stop)
            if s.step is not None:
                self.expr(s.step)
            self.push()
            self.declare(s.var, "loop")
            self.block(s.body, scope=False)
            self.pop()
        elif t == "forin":
            for e in s.exprs:
                self.expr(e)
            self.push()
            for n in s.names:
                self.declare(n, "loop")
            self.block(s.body, scope=False)
            self.pop()
        elif t == "return":
            for e in s.exprs:
                self.expr(e)

    def expr(self, e):
        t = e.t
        if t == "name":
            b = self.lookup(e.name)
            e.binding = b
            if b is not None:
                b.uses += 1
                e.name = b.new
            else:
                e.name = self.field(e.name) if OBF_RE.match(e.name) and self.rename else e.name
        elif t == "index":
            self.expr(e.obj)
            if e.key.t == "str" and e.dot:
                text = e.key.v.decode("latin-1")
                e.key.v = self.field(text).encode("latin-1")
            else:
                self.expr(e.key)
        elif t == "call":
            self.expr(e.fn)
            for a in e.args:
                self.expr(a)
        elif t == "method":
            self.expr(e.obj)
            e.name = self.field(e.name)
            for a in e.args:
                self.expr(a)
        elif t == "func":
            self.push()
            for p in e.params:
                if p.name == "self" and e.is_method:
                    p.binding = Binding("self", "self")
                    self.scopes[-1]["self"] = p.binding
                else:
                    self.declare(p, "param")
            self.block(e.body, scope=False)
            self.pop()
        elif t == "table":
            items = []
            for kind, k, v in e.items:
                if kind == "name":
                    k = self.field(k)
                elif kind == "expr":
                    self.expr(k)
                self.expr(v)
                items.append((kind, k, v))
            e.items = items
        elif t == "binop":
            self.expr(e.l)
            self.expr(e.r)
        elif t == "unop":
            self.expr(e.e)
        elif t == "paren":
            self.expr(e.e)


def is_pure(e):
    t = e.t
    if t in ("nil", "bool", "num", "str", "name"):
        return True
    if t == "paren":
        return is_pure(e.e)
    if t == "unop":
        return is_pure(e.e)
    if t == "binop":
        return is_pure(e.l) and is_pure(e.r)
    if t == "call":
        f = e.fn
        if f.t == "index" and f.dot and f.obj.t == "name" and f.key.t == "str":
            b = getattr(f.obj, "binding", None)
            lib = b.alias if b is not None else f.obj.name
            if lib in PURE_LIBS and f.key.v.decode("latin-1") in PURE_LIBS[lib]:
                return all(is_pure(a) for a in e.args)
    return False


def const_truth(e):
    if e.t == "nil":
        return False
    if e.t == "bool":
        return e.v
    return None


def simplify(stmts):
    changed = False
    out = []
    for s in stmts:
        t = s.t
        if t == "func":
            continue
        if t in ("localfunc", "funcstat"):
            c = simplify_func(s.func)
            changed = changed or c
        elif t in ("do", "while", "repeat", "fornum", "forin"):
            s.body, c = simplify(s.body)
            changed = changed or c
        elif t == "if":
            kept = []
            for cond, body in s.clauses:
                body, c = simplify(body)
                changed = changed or c
                truth = const_truth(cond)
                if truth is False:
                    changed = True
                    continue
                kept.append((cond, body))
                if truth is True:
                    if s.orelse is not None:
                        changed = True
                    s.orelse = None
                    break
            if s.orelse is not None:
                s.orelse, c = simplify(s.orelse)
                changed = changed or c
            if not kept:
                changed = True
                if s.orelse:
                    out.append(type(s)("do", body=s.orelse))
                continue
            if len(kept) == 1 and const_truth(kept[0][0]) is True:
                changed = True
                out.append(type(s)("do", body=kept[0][1]))
                continue
            s.clauses = kept
        elif t == "local":
            if all(n.binding.uses == 0 for n in s.names) and all(is_pure(x) for x in s.exprs):
                changed = True
                continue
            for x in s.exprs:
                if x.t == "func":
                    changed = simplify_func(x) or changed
        elif t in ("assign", "return", "callstat"):
            pass
        if t == "do" and not s.body:
            changed = True
            continue
        out.append(s)
    return out, changed


def simplify_func(f):
    f.body, c = simplify(f.body)
    return c


def quote(b):
    out = ['"']
    for byte in b:
        ch = chr(byte)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif byte < 32 or byte == 127:
            out.append("\\%03d" % byte)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class Printer:
    def __init__(self, indent="    "):
        self.indent = indent
        self.level = 0
        self.lines = []

    def pad(self, level=None):
        return self.indent * (self.level if level is None else level)

    def emit(self, text):
        self.lines.append(self.pad() + text)

    def block(self, stmts):
        first = True
        for s in stmts:
            before = len(self.lines)
            self.stmt(s)
            if not first and len(self.lines) > before:
                head = self.lines[before].lstrip()
                if head.startswith("("):
                    self.lines[before] = self.pad() + ";" + head
            first = False

    def body(self, stmts):
        self.level += 1
        self.block(stmts)
        self.level -= 1

    def params(self, f):
        names = [p.name for p in f.params[1 if f.is_method else 0:]]
        if f.vararg:
            names.append("...")
        return ", ".join(names)

    def stmt(self, s):
        t = s.t
        if t == "local":
            names = ", ".join(n.name for n in s.names)
            if s.exprs:
                self.emit("local %s = %s" % (names, ", ".join(self.expr(e) for e in s.exprs)))
            else:
                self.emit("local " + names)
        elif t == "localfunc":
            self.emit("local function %s(%s)" % (s.name.name, self.params(s.func)))
            self.body(s.func.body)
            self.emit("end")
        elif t == "funcstat":
            target = self.expr(s.target)
            if s.method is not None:
                target += ":" + s.method
            self.emit("function %s(%s)" % (target, self.params(s.func)))
            self.body(s.func.body)
            self.emit("end")
        elif t == "assign":
            self.emit("%s = %s" % (", ".join(self.expr(x) for x in s.targets), ", ".join(self.expr(e) for e in s.exprs)))
        elif t == "callstat":
            self.emit(self.expr(s.call))
        elif t == "do":
            self.emit("do")
            self.body(s.body)
            self.emit("end")
        elif t == "while":
            self.emit("while %s do" % self.expr(s.cond))
            self.body(s.body)
            self.emit("end")
        elif t == "repeat":
            self.emit("repeat")
            self.body(s.body)
            self.emit("until %s" % self.expr(s.cond))
        elif t == "if":
            for i, (c, b) in enumerate(s.clauses):
                self.emit("%s %s then" % ("if" if i == 0 else "elseif", self.expr(c)))
                self.body(b)
            if s.orelse:
                self.emit("else")
                self.body(s.orelse)
            self.emit("end")
        elif t == "fornum":
            parts = [self.expr(s.start), self.expr(s.stop)]
            if s.step is not None:
                parts.append(self.expr(s.step))
            self.emit("for %s = %s do" % (s.var.name, ", ".join(parts)))
            self.body(s.body)
            self.emit("end")
        elif t == "forin":
            self.emit("for %s in %s do" % (", ".join(n.name for n in s.names), ", ".join(self.expr(e) for e in s.exprs)))
            self.body(s.body)
            self.emit("end")
        elif t == "return":
            self.emit("return " + ", ".join(self.expr(e) for e in s.exprs) if s.exprs else "return")
        elif t == "break":
            self.emit("break")

    def func_expr(self, f):
        header = "function(%s)" % self.params(f)
        saved = self.lines
        self.lines = []
        self.level += 1
        self.block(f.body)
        self.level -= 1
        body = self.lines
        self.lines = saved
        if not body:
            return header + " end"
        return header + "\n" + "\n".join(body) + "\n" + self.pad() + "end"

    def expr(self, e):
        t = e.t
        if t == "nil":
            return "nil"
        if t == "bool":
            return "true" if e.v else "false"
        if t == "num":
            return e.raw
        if t == "str":
            return quote(e.v)
        if t == "vararg":
            return "..."
        if t == "name":
            return e.name
        if t == "paren":
            return "(" + self.expr(e.e) + ")"
        if t == "index":
            obj = self.expr(e.obj)
            if e.key.t == "str":
                text = e.key.v.decode("latin-1")
                if IDENT_RE.match(text) and text not in KEYWORDS:
                    return "%s.%s" % (obj, text)
            return "%s[%s]" % (obj, self.expr(e.key))
        if t == "call":
            return "%s(%s)" % (self.expr(e.fn), ", ".join(self.expr(a) for a in e.args))
        if t == "method":
            return "%s:%s(%s)" % (self.expr(e.obj), e.name, ", ".join(self.expr(a) for a in e.args))
        if t == "func":
            return self.func_expr(e)
        if t == "table":
            return self.table_expr(e)
        if t == "unop":
            inner = self.expr(e.e)
            if e.op == "not":
                return "not " + inner
            if e.op == "-" and inner.startswith("-"):
                return "- " + inner
            return e.op + inner
        if t == "binop":
            return "%s %s %s" % (self.expr(e.l), e.op, self.expr(e.r))
        return "nil"

    def table_expr(self, e):
        if not e.items:
            return "{}"
        self.level += 1
        parts = []
        for kind, k, v in e.items:
            vs = self.expr(v)
            if kind == "pos":
                parts.append(vs)
            elif kind == "name":
                parts.append("%s = %s" % (k, vs))
            else:
                parts.append("[%s] = %s" % (self.expr(k), vs))
        self.level -= 1
        flat = "{" + ", ".join(parts) + "}"
        if len(flat) <= 100 and "\n" not in flat:
            return flat
        inner = self.pad(self.level + 1)
        if all(len(p) <= 8 and "\n" not in p for p in parts):
            rows = [", ".join(parts[i:i + 20]) for i in range(0, len(parts), 20)]
            return "{\n" + ",\n".join(inner + r for r in rows) + "\n" + self.pad() + "}"
        return "{\n" + ",\n".join(inner + p for p in parts) + "\n" + self.pad() + "}"


def reconstruct(code, rename=True):
    text = code.decode("latin-1") if isinstance(code, (bytes, bytearray)) else code
    try:
        tree = parse(text)
    except (ParseError, LexError):
        try:
            cleaned = strip_comments(text)
        except (ParseError, LexError):
            cleaned = text.strip()
        return (cleaned + "\n").encode("latin-1", errors="replace")
    taken = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    for _ in range(12):
        Resolver(False, taken).block(tree.body, scope=False)
        tree.body, changed = simplify(tree.body)
        if not changed:
            break
    Resolver(rename, taken).block(tree.body, scope=False)
    printer = Printer()
    printer.block(tree.body)
    return ("\n".join(printer.lines) + "\n").encode("latin-1")

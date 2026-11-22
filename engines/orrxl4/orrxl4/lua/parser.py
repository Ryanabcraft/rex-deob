from .lexer import tokenize


class ParseError(Exception):
    pass


class Node:
    def __init__(self, t, **fields):
        self.t = t
        self.__dict__.update(fields)

    def __repr__(self):
        return "Node(%s)" % ", ".join("%s=%r" % kv for kv in self.__dict__.items())


BINARY_PRIORITY = {
    "or": (1, 1), "and": (2, 2),
    "<": (3, 3), ">": (3, 3), "<=": (3, 3), ">=": (3, 3), "~=": (3, 3), "==": (3, 3),
    "|": (4, 4), "~": (5, 5), "&": (6, 6), "<<": (7, 7), ">>": (7, 7),
    "..": (9, 8), "+": (10, 10), "-": (10, 10),
    "*": (11, 11), "/": (11, 11), "//": (11, 11), "%": (11, 11),
    "^": (14, 13),
}
UNARY_PRIORITY = 12
BLOCK_END = {"end", "else", "elseif", "until"}
COMPOUND = {"+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%", "^=": "^", "..=": ".."}


class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def check(self, value):
        t = self.toks[self.i]
        return t.value == value and t.kind in ("kw", "op")

    def accept(self, value):
        if self.check(value):
            self.i += 1
            return True
        return False

    def expect(self, value):
        t = self.toks[self.i]
        if not (t.value == value and t.kind in ("kw", "op")):
            raise ParseError("expected %r near %r at line %d" % (value, t.value, t.line))
        self.i += 1
        return t

    def expect_name(self):
        t = self.toks[self.i]
        if t.kind != "name":
            raise ParseError("expected name near %r at line %d" % (t.value, t.line))
        self.i += 1
        return t

    def parse_chunk(self):
        body = self.block()
        if self.peek().kind != "eof":
            t = self.peek()
            raise ParseError("unexpected %r at line %d" % (t.value, t.line))
        return Node("func", params=[], vararg=True, body=body, is_method=False, name=None)

    def block(self):
        stmts = []
        while True:
            t = self.peek()
            if t.kind == "eof" or (t.kind == "kw" and t.value in BLOCK_END):
                break
            if t.kind == "kw" and t.value == "return":
                self.next()
                exprs = []
                t2 = self.peek()
                if not (t2.kind == "eof" or (t2.kind == "kw" and t2.value in BLOCK_END) or self.check(";")):
                    exprs = self.exprlist()
                self.accept(";")
                stmts.append(Node("return", exprs=exprs))
                break
            s = self.statement()
            if s is not None:
                stmts.append(s)
        return stmts

    def statement(self):
        t = self.peek()
        if t.kind == "op" and t.value == ";":
            self.next()
            return None
        if t.kind == "op" and t.value == "::":
            raise ParseError("labels are not supported")
        if t.kind == "kw":
            v = t.value
            if v == "local":
                return self.local_stat()
            if v == "function":
                return self.function_stat()
            if v == "if":
                return self.if_stat()
            if v == "while":
                self.next()
                cond = self.expr()
                self.expect("do")
                body = self.block()
                self.expect("end")
                return Node("while", cond=cond, body=body)
            if v == "do":
                self.next()
                body = self.block()
                self.expect("end")
                return Node("do", body=body)
            if v == "for":
                return self.for_stat()
            if v == "repeat":
                self.next()
                body = self.block()
                self.expect("until")
                cond = self.expr()
                return Node("repeat", body=body, cond=cond)
            if v == "break":
                self.next()
                return Node("break")
            if v == "goto":
                raise ParseError("goto is not supported")
        e = self.suffixed()
        t = self.peek()
        if t.kind == "op" and (t.value == "=" or t.value == ","):
            targets = [e]
            while self.accept(","):
                targets.append(self.suffixed())
            self.expect("=")
            exprs = self.exprlist()
            for tg in targets:
                if tg.t not in ("name", "index"):
                    raise ParseError("cannot assign at line %d" % t.line)
            return Node("assign", targets=targets, exprs=exprs)
        if t.kind == "op" and t.value in COMPOUND:
            self.next()
            rhs = self.expr()
            return Node("assign", targets=[e], exprs=[Node("binop", op=COMPOUND[t.value], l=e, r=rhs)])
        if e.t not in ("call", "method"):
            raise ParseError("syntax error near %r at line %d" % (t.value, t.line))
        return Node("callstat", call=e)

    def local_stat(self):
        self.expect("local")
        if self.accept("function"):
            name = self.expect_name()
            func = self.func_body(False, name.value)
            return Node("localfunc", name=Node("name", name=name.value), func=func)
        names = []
        while True:
            t = self.expect_name()
            names.append(Node("name", name=t.value))
            if self.accept("<"):
                self.expect_name()
                self.expect(">")
            if not self.accept(","):
                break
        exprs = []
        if self.accept("="):
            exprs = self.exprlist()
        return Node("local", names=names, exprs=exprs)

    def function_stat(self):
        self.expect("function")
        t = self.expect_name()
        target = Node("name", name=t.value)
        method = None
        label = t.value
        while self.check("."):
            self.next()
            k = self.expect_name()
            target = Node("index", obj=target, key=Node("str", v=k.value.encode("latin-1")), dot=True)
            label = k.value
        if self.accept(":"):
            k = self.expect_name()
            method = k.value
            label = k.value
        func = self.func_body(method is not None, label)
        return Node("funcstat", target=target, method=method, func=func)

    def func_body(self, is_method, name=None):
        self.expect("(")
        params = []
        vararg = False
        if is_method:
            params.append(Node("name", name="self"))
        if not self.check(")"):
            while True:
                if self.accept("..."):
                    vararg = True
                    break
                t = self.expect_name()
                params.append(Node("name", name=t.value))
                if not self.accept(","):
                    break
        self.expect(")")
        body = self.block()
        self.expect("end")
        return Node("func", params=params, vararg=vararg, body=body, is_method=is_method, name=name)

    def if_stat(self):
        self.expect("if")
        clauses = []
        cond = self.expr()
        self.expect("then")
        clauses.append((cond, self.block()))
        orelse = None
        while True:
            if self.accept("elseif"):
                c = self.expr()
                self.expect("then")
                clauses.append((c, self.block()))
            elif self.accept("else"):
                orelse = self.block()
                self.expect("end")
                break
            else:
                self.expect("end")
                break
        return Node("if", clauses=clauses, orelse=orelse)

    def for_stat(self):
        self.expect("for")
        first = self.expect_name()
        if self.check("="):
            self.next()
            start = self.expr()
            self.expect(",")
            stop = self.expr()
            step = None
            if self.accept(","):
                step = self.expr()
            self.expect("do")
            body = self.block()
            self.expect("end")
            return Node("fornum", var=Node("name", name=first.value), start=start, stop=stop, step=step, body=body)
        names = [Node("name", name=first.value)]
        while self.accept(","):
            names.append(Node("name", name=self.expect_name().value))
        self.expect("in")
        exprs = self.exprlist()
        self.expect("do")
        body = self.block()
        self.expect("end")
        return Node("forin", names=names, exprs=exprs, body=body)

    def exprlist(self):
        exprs = [self.expr()]
        while self.accept(","):
            exprs.append(self.expr())
        return exprs

    def primary(self):
        t = self.peek()
        if t.kind == "name":
            self.next()
            return Node("name", name=t.value)
        if t.kind == "op" and t.value == "(":
            self.next()
            e = self.expr()
            self.expect(")")
            return Node("paren", e=e)
        raise ParseError("unexpected symbol %r at line %d" % (t.value, t.line))

    def suffixed(self):
        e = self.primary()
        while True:
            t = self.peek()
            if t.kind == "op":
                v = t.value
                if v == ".":
                    self.next()
                    k = self.expect_name()
                    e = Node("index", obj=e, key=Node("str", v=k.value.encode("latin-1")), dot=True)
                    continue
                if v == "[":
                    self.next()
                    k = self.expr()
                    self.expect("]")
                    e = Node("index", obj=e, key=k, dot=False)
                    continue
                if v == ":":
                    self.next()
                    k = self.expect_name()
                    args = self.call_args()
                    e = Node("method", obj=e, name=k.value, args=args)
                    continue
                if v == "(" or v == "{":
                    e = Node("call", fn=e, args=self.call_args())
                    continue
            elif t.kind == "str":
                e = Node("call", fn=e, args=self.call_args())
                continue
            return e

    def call_args(self):
        t = self.peek()
        if t.kind == "str":
            self.next()
            return [Node("str", v=t.value)]
        if t.kind == "op" and t.value == "{":
            return [self.table()]
        self.expect("(")
        args = []
        if not self.check(")"):
            args = self.exprlist()
        self.expect(")")
        return args

    def table(self):
        self.expect("{")
        items = []
        while not self.check("}"):
            t = self.peek()
            if t.kind == "op" and t.value == "[":
                self.next()
                k = self.expr()
                self.expect("]")
                self.expect("=")
                items.append(("expr", k, self.expr()))
            elif t.kind == "name" and self.toks[self.i + 1].kind == "op" and self.toks[self.i + 1].value == "=":
                self.next()
                self.next()
                items.append(("name", t.value, self.expr()))
            else:
                items.append(("pos", None, self.expr()))
            if not (self.accept(",") or self.accept(";")):
                break
        self.expect("}")
        return Node("table", items=items)

    def simple(self):
        t = self.peek()
        k = t.kind
        if k == "num":
            self.next()
            return Node("num", v=t.value, raw=t.raw)
        if k == "str":
            self.next()
            return Node("str", v=t.value)
        if k == "kw":
            if t.value == "nil":
                self.next()
                return Node("nil")
            if t.value == "true":
                self.next()
                return Node("bool", v=True)
            if t.value == "false":
                self.next()
                return Node("bool", v=False)
            if t.value == "function":
                self.next()
                return self.func_body(False)
        if k == "op":
            if t.value == "...":
                self.next()
                return Node("vararg")
            if t.value == "{":
                return self.table()
        return self.suffixed()

    def expr(self, limit=0):
        t = self.peek()
        if (t.kind == "kw" and t.value == "not") or (t.kind == "op" and t.value in ("-", "#", "~")):
            self.next()
            operand = self.expr(UNARY_PRIORITY)
            left = Node("unop", op=t.value, e=operand)
        else:
            left = self.simple()
        while True:
            t = self.peek()
            if t.kind not in ("op", "kw"):
                break
            pr = BINARY_PRIORITY.get(t.value)
            if pr is None or pr[0] <= limit:
                break
            self.next()
            right = self.expr(pr[1])
            left = Node("binop", op=t.value, l=left, r=right)
        return left


def parse(source):
    tokens, _ = tokenize(source)
    return Parser(tokens).parse_chunk()

import re

KEYWORDS = frozenset(
    "and break do else elseif end false for function goto if in local nil not or repeat return then true until while".split()
)

OPERATORS = [
    "...", "..=", "..", "==", "~=", "<=", ">=", "//", "::", "<<", ">>",
    "+=", "-=", "*=", "/=", "%=", "^=",
    "+", "-", "*", "/", "%", "^", "#", "&", "~", "|", "<", ">", "=",
    "(", ")", "{", "}", "[", "]", ";", ":", ",", ".",
]

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUM_RE = re.compile(
    r"0[xX][0-9a-fA-F_]*(?:\.[0-9a-fA-F_]*)?(?:[pP][+-]?\d+)?"
    r"|0[bB][01_]+"
    r"|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?"
)
LONG_OPEN_RE = re.compile(r"\[(=*)\[")
SIMPLE_ESCAPES = {
    "n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12, "v": 11,
    "\\": 92, '"': 34, "'": 39,
}


class LexError(Exception):
    pass


class Token:
    __slots__ = ("kind", "value", "raw", "pos", "end", "line")

    def __init__(self, kind, value, raw, pos, end, line):
        self.kind = kind
        self.value = value
        self.raw = raw
        self.pos = pos
        self.end = end
        self.line = line

    def __repr__(self):
        return "Token(%s, %r)" % (self.kind, self.value)


def decode_escapes(body, line):
    out = bytearray()
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c != "\\":
            out.append(ord(c))
            i += 1
            continue
        i += 1
        if i >= n:
            raise LexError("bad escape at line %d" % line)
        c = body[i]
        if c in SIMPLE_ESCAPES:
            out.append(SIMPLE_ESCAPES[c])
            i += 1
        elif c == "\n":
            out.append(10)
            i += 1
            if i < n and body[i] == "\r":
                i += 1
        elif c == "\r":
            out.append(10)
            i += 1
            if i < n and body[i] == "\n":
                i += 1
        elif c == "x":
            h = body[i + 1:i + 3]
            if len(h) != 2 or not re.fullmatch(r"[0-9a-fA-F]{2}", h):
                raise LexError("bad hex escape at line %d" % line)
            out.append(int(h, 16))
            i += 3
        elif c == "z":
            i += 1
            while i < n and body[i] in " \t\r\n\f\v":
                i += 1
        elif c.isdigit():
            j = i
            while j < n and j < i + 3 and body[j].isdigit():
                j += 1
            v = int(body[i:j])
            if v > 255:
                raise LexError("decimal escape too large at line %d" % line)
            out.append(v)
            i = j
        elif c == "u":
            m = re.compile(r"u\{([0-9a-fA-F]+)\}").match(body, i)
            if not m:
                raise LexError("bad unicode escape at line %d" % line)
            out.extend(chr(int(m.group(1), 16)).encode("utf-8", "surrogatepass"))
            i = m.end()
        else:
            raise LexError("invalid escape \\%s at line %d" % (c, line))
    return bytes(out)


def parse_number(raw):
    text = raw.replace("_", "")
    low = text.lower()
    if low.startswith("0x"):
        if "." in low or "p" in low:
            mant, _, exp = low[2:].partition("p")
            ip, _, fp = mant.partition(".")
            value = int(ip or "0", 16) + (int(fp, 16) / (16 ** len(fp)) if fp else 0)
            return float(value * (2 ** int(exp or "0")))
        return int(low, 16)
    if low.startswith("0b"):
        return int(low[2:], 2)
    if "." in low or "e" in low:
        return float(low)
    return int(low)


def tokenize(source):
    tokens = []
    comments = []
    pos = 0
    line = 1
    n = len(source)
    while pos < n:
        c = source[pos]
        if c == "\n":
            line += 1
            pos += 1
            continue
        if c in " \t\r\f\v":
            pos += 1
            continue
        if c == "-" and source.startswith("--", pos):
            start = pos
            m = LONG_OPEN_RE.match(source, pos + 2)
            if m:
                close = "]" + m.group(1) + "]"
                e = source.find(close, m.end())
                if e < 0:
                    raise LexError("unfinished long comment at line %d" % line)
                end = e + len(close)
            else:
                e = source.find("\n", pos)
                end = n if e < 0 else e
            line += source.count("\n", start, end)
            comments.append((start, end))
            pos = end
            continue
        if c.isalpha() or c == "_":
            m = NAME_RE.match(source, pos)
            word = m.group(0)
            kind = "kw" if word in KEYWORDS else "name"
            tokens.append(Token(kind, word, word, pos, m.end(), line))
            pos = m.end()
            continue
        if c.isdigit() or (c == "." and pos + 1 < n and source[pos + 1].isdigit()):
            m = NUM_RE.match(source, pos)
            raw = m.group(0)
            tokens.append(Token("num", parse_number(raw), raw, pos, m.end(), line))
            pos = m.end()
            continue
        if c == '"' or c == "'":
            start = pos
            i = pos + 1
            while True:
                if i >= n:
                    raise LexError("unfinished string at line %d" % line)
                ch = source[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == c:
                    break
                if ch == "\n":
                    raise LexError("unfinished string at line %d" % line)
                i += 1
            body = source[pos + 1:i]
            tokens.append(Token("str", decode_escapes(body, line), source[start:i + 1], start, i + 1, line))
            line += body.count("\n")
            pos = i + 1
            continue
        if c == "[":
            m = LONG_OPEN_RE.match(source, pos)
            if m:
                close = "]" + m.group(1) + "]"
                e = source.find(close, m.end())
                if e < 0:
                    raise LexError("unfinished long string at line %d" % line)
                body = source[m.end():e]
                if body.startswith("\r\n"):
                    body = body[2:]
                elif body.startswith("\n"):
                    body = body[1:]
                end = e + len(close)
                tokens.append(Token("str", body.encode("latin-1"), source[pos:end], pos, end, line))
                line += source.count("\n", pos, end)
                pos = end
                continue
        if source.startswith("!=", pos):
            tokens.append(Token("op", "~=", "!=", pos, pos + 2, line))
            pos += 2
            continue
        for op in OPERATORS:
            if source.startswith(op, pos):
                tokens.append(Token("op", op, op, pos, pos + len(op), line))
                pos += len(op)
                break
        else:
            raise LexError("unexpected character %r at line %d" % (c, line))
    tokens.append(Token("eof", "<eof>", "", n, n, line))
    return tokens, comments


def strip_comments(source):
    _, comments = tokenize(source)
    if not comments:
        return source
    out = []
    last = 0
    for start, end in comments:
        out.append(source[last:start])
        last = end
    out.append(source[last:])
    text = "".join(out)
    return "\n".join(l.rstrip() for l in text.split("\n") if l.strip())

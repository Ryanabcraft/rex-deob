"""Final readability pass over devirtualized + luaclean'd Luau.

Only rewrites that keep the program's behaviour identical:
  * drops `payload_fn_N` closures nothing references (Luraph runtime leftovers)
  * folds the lifter's signal pattern back into `X.Event:Connect(function() ... end)`
      local a = X.Event / local b = a / local c = a.Connect / local function f() ... end / c(b, f)
    (only when a, b, c and f are used nowhere else)
  * Color3.new(r, g, b) whose channels are exact n/255 -> Color3.fromRGB(n, n, n)
  * collapses runs of blank lines
Every rewrite is re-parsed with luau-ast; if the result does not parse, the input
is written back unchanged.

usage: python polish.py <luau-ast> <in.lua> <out.lua>
"""
import re
import subprocess
import sys
import tempfile
import os

IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def count_word(text, name):
    return len(re.findall(r"(?<![A-Za-z0-9_.:])" + re.escape(name) + r"(?![A-Za-z0-9_])", text))


def drop_unused_payload_fns(text):
    # Decide every removal against the ORIGINAL text, never against text already
    # trimmed. In a Luraph payload the closures are wired through upvalues (upvN),
    # not by name, so "the name appears nowhere else" is not proof of dead code --
    # and dropping one function can be exactly what makes the next one look
    # unreferenced, a cascade that used to delete the whole payload.
    lines = text.split("\n")
    spans, i = {}, 0
    while i < len(lines):
        m = re.match(r"^(\s*)local function (payload_fn_\d+)\(", lines[i])
        if m:
            ind, name = m.group(1), m.group(2)
            j = i + 1
            while j < len(lines) and lines[j] != ind + "end":
                j += 1
            if j < len(lines):
                rest = "\n".join(lines[:i] + lines[j + 1:])
                if count_word(rest, name) == 0:
                    spans[i] = j
                    i = j + 1
                    continue
        i += 1
    if not spans:
        return text
    out, removed, i = [], 0, 0
    while i < len(lines):
        if i in spans:
            j = spans[i]
            removed += len("\n".join(lines[i:j + 1]))
            if out and re.match(r"^\s*-- function created", out[-1]):
                out.pop()
            i = j + 1
            while i < len(lines) and lines[i].strip() == "":
                i += 1
            continue
        out.append(lines[i])
        i += 1
    # This pass trims leftovers, it never trims the payload: losing more than a
    # quarter of the file means the heuristic guessed wrong, so keep the input.
    if removed > len(text) * 0.25:
        return text
    return "\n".join(out)


def fold_connects(text):
    changed = True
    while changed:
        changed = False
        lines = text.split("\n")
        for i in range(len(lines) - 4):
            m1 = re.match(r"^(\s*)local (" + IDENT + r") = (.+)$", lines[i])
            if not m1:
                continue
            ind, a, sig = m1.group(1), m1.group(2), m1.group(3)
            m2 = re.match("^" + re.escape(ind) + r"local (" + IDENT + r") = " + re.escape(a) + "$", lines[i + 1])
            m3 = re.match("^" + re.escape(ind) + r"local (" + IDENT + r") = " + re.escape(a) + r"\.(Connect|Once|ConnectParallel)$",
                          lines[i + 2])
            if not (m2 and m3):
                continue
            b, c, method = m2.group(1), m3.group(1), m3.group(2)
            k = i + 3
            while k < len(lines) and lines[k].strip() == "":
                k += 1
            m4 = re.match("^" + re.escape(ind) + r"local function (" + IDENT + r")\((.*)\)$", lines[k]) if k < len(lines) else None
            if not m4:
                continue
            f, params = m4.group(1), m4.group(2)
            e = k + 1
            while e < len(lines) and lines[e] != ind + "end":
                e += 1
            if e >= len(lines):
                continue
            q = e + 1
            while q < len(lines) and lines[q].strip() == "":
                q += 1
            if q >= len(lines) or lines[q] != "%s%s(%s, %s)" % (ind, c, b, f):
                continue
            body = lines[k + 1:e]
            others = "\n".join(lines[:i] + lines[q + 1:]) + "\n" + "\n".join(body)
            if any(count_word(others, n) for n in (a, b, c, f)):
                continue
            new = ["%s%s:%s(function(%s)" % (ind, sig, method, params)] + body + [ind + "end)"]
            lines[i:q + 1] = new
            text = "\n".join(lines)
            changed = True
            break
    return text


def color3_rgb(text):
    num = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"

    def rep(m):
        vals = [float(m.group(i)) for i in (1, 2, 3)]
        ints = [round(v * 255) for v in vals]
        if all(0 <= n <= 255 and abs(v * 255 - n) < 1e-4 for v, n in zip(vals, ints)):
            if all(n in (0, 255) for n in ints):
                return "Color3.new(%s)" % ", ".join("1" if n else "0" for n in ints)
            return "Color3.fromRGB(%d, %d, %d)" % tuple(ints)
        return m.group(0)

    return re.sub(r"Color3\.new\(" + num + r", " + num + r", " + num + r"\)", rep, text)


KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
            "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
            "continue", "export", "type"}
# register temporaries the lifter names after their kind (v12, n3, tbl4, flag2 ...)
TEMP = re.compile(r"^(?:v|n|tbl|flag|str|fn|num|obj|val)\d*$")
CLOSERS = ("end", "else", "elseif", "until", "}", ")", "]")


def strip_code(line):
    """The line with string-literal contents and comments blanked (same length)."""
    out, i, n = [], 0, len(line)
    while i < n:
        ch = line[i]
        if ch in "\"'`":
            q = ch
            out.append(ch)
            i += 1
            while i < n and line[i] != q:
                if line[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append(" ")
                i += 1
            if i < n:
                out.append(q)
                i += 1
            continue
        if line.startswith("--", i):
            out.append(" " * (n - i))
            break
        if line.startswith("[[", i) or line.startswith("[=", i):
            return None          # long brackets: let the caller skip this line
        out.append(ch)
        i += 1
    return "".join(out)


def word_spans(code, name):
    return [m.start() for m in re.finditer(r"(?<![A-Za-z0-9_.:])" + re.escape(name) + r"(?![A-Za-z0-9_])", code)]


def indent_of(line):
    return len(line) - len(line.lstrip(" \t"))


def block_end(lines, i, ind):
    """First line after i that leaves the block the statement on line i lives in."""
    k = i + 1
    while k < len(lines):
        ln = lines[k]
        if ln.strip():
            d = indent_of(ln)
            if d < ind:
                return k
            if d == ind and re.match(r"(end|else|elseif|until)\b", ln.strip()):
                return k
        k += 1
    return k


def assigns(code, name):
    """True when `code` (one stripped line) may rebind `name`."""
    w = r"(?<![A-Za-z0-9_.:])" + re.escape(name) + r"(?![A-Za-z0-9_])"
    if re.search(r"\blocal\b[^=]*" + w, code) or re.search(r"\bfunction\b[^)]*" + w, code):
        return True
    if re.search(r"\bfor\b[^=]*" + w + r"[^=]*(=|\bin\b)", code):
        return True
    # `name = ...`, `a, name = ...`, `name += ...`
    if re.search(r"^\s*(?:[\w.\[\]\"']+\s*,\s*)*" + w + r"\s*(?:,\s*[\w.\[\]\"']+\s*)*(?:[-+*/%^]|\.\.|//)?=(?!=)", code):
        return True
    return False


def inline_aliases(text):
    """`local v12 = MathClamp` read exactly once, later in the same block and at
    the same nesting level, with MathClamp not rebound in between -> use the
    original name there and drop the copy. (Register copies the VM needed; the
    original source called the name directly, which evaluates it first anyway.)"""
    for _ in range(6):
        lines = text.split("\n")
        codes = [strip_code(l) for l in lines]
        changed = False
        drop = set()
        i = 0
        while i < len(lines):
            m = re.match(r"^(\s*)local (" + IDENT + r") = (" + IDENT + r"|-?\d+(?:\.\d+)?)$", lines[i])
            if not m or i in drop:
                i += 1
                continue
            ind, t, x = len(m.group(1)), m.group(2), m.group(3)
            if not TEMP.match(t) or t == x or (x in KEYWORDS and x not in ("nil", "true", "false")):
                i += 1
                continue
            end = block_end(lines, i, ind)
            hits = []
            ok = True
            for k in range(i + 1, end):
                c = codes[k]
                if c is None:
                    ok = False
                    break
                for pos in word_spans(c, t):
                    hits.append((k, pos))
                if len(hits) > 1:
                    ok = False
                    break
            if not ok or len(hits) != 1:
                i += 1
                continue
            u, pos = hits[0]
            cu = codes[u]
            if indent_of(lines[u]) != ind or u in drop:
                i += 1
                continue
            if assigns(cu, t) or re.match(r"\s*" + re.escape(t) + r"\s*(?:,|=(?!=)|[-+*/%^]=|\.\.=)", cu[pos:]):
                i += 1
                continue
            if not re.match(r"^[A-Za-z_]", x) and re.match(r"\s*[.:\[(]", cu[pos + len(t):]):
                i += 1          # never index/call a literal (`0.x`, `nil(...)`)
                continue
            if x not in ("nil", "true", "false") and re.match(r"^[A-Za-z_]", x):
                if any(codes[k] is None or assigns(codes[k], x) for k in range(i + 1, u + 1)):
                    i += 1
                    continue
            lines[u] = lines[u][:pos] + x + lines[u][pos + len(t):]
            codes[u] = cu[:pos] + x + cu[pos + len(t):]
            drop.add(i)
            changed = True
            i += 1
        if not changed:
            break
        text = "\n".join(l for k, l in enumerate(lines) if k not in drop)
    return text


def merge_decl_assign(text):
    """`local x` immediately followed by `x = <expr>` -> `local x = <expr>`, when
    the expression (all its lines) never mentions x (so recursion/self-reference
    keeps binding to the same variable)."""
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)local (" + IDENT + r")(?: = nil)?$", lines[i])
        if m and i + 1 < len(lines):
            ind, t = m.group(1), m.group(2)
            m2 = re.match("^" + re.escape(ind) + re.escape(t) + r" = (.+)$", lines[i + 1])
            if m2:
                j = i + 1
                first = strip_code(lines[j])
                if first is not None:
                    opens = len(re.findall(r"[({\[]|\bfunction\b|\bdo\b|\bthen\b", first))
                    closes = len(re.findall(r"[)}\]]|\bend\b", first))
                    if opens > closes:     # multi-line value: runs to the closer at our indent
                        j += 1
                        while j < len(lines) and not (indent_of(lines[j]) == len(ind) and lines[j].strip()
                                                      and lines[j].strip().startswith(CLOSERS)):
                            j += 1
                    if j < len(lines):
                        body = [strip_code(l) for l in lines[i + 1:j + 1]]
                        if all(b is not None for b in body):
                            refs = sum(len(word_spans(b, t)) for b in body)
                            if refs == 1:
                                out.append(ind + "local " + t + " = " + m2.group(1))
                                out.extend(lines[i + 2:j + 1])
                                i = j + 1
                                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


BLOCKY = re.compile(r"^\s*(if|for|while|repeat|do|local function|function|return|--|end|until|else)\b")


def collapse_blank(text):
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    # a lone blank line between two plain statements is where luaclean deleted
    # junk: close the gap (blank lines around blocks are kept)
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        if ln.strip() == "" and 0 < i < len(lines) - 1:
            prev, nxt = out[-1] if out else "", lines[i + 1]
            if (prev.strip() and nxt.strip() and not BLOCKY.match(prev) and not BLOCKY.match(nxt)
                    and not prev.rstrip().endswith(("then", "do", "(", "{", ")")) or
                    (prev.strip() and nxt.strip() and not BLOCKY.match(prev) and not BLOCKY.match(nxt)
                     and prev.rstrip().endswith(")") and not prev.rstrip().endswith("end)"))):
                continue
        out.append(ln)
    return "\n".join(out).strip("\n") + "\n"


def parses(ast_exe, text):
    fd, path = tempfile.mkstemp(suffix=".luau")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        r = subprocess.run([ast_exe, path], capture_output=True)
        return r.returncode == 0
    finally:
        os.unlink(path)


def main():
    ast_exe, src, dst = sys.argv[1:4]
    text = open(src, encoding="utf-8", errors="surrogateescape").read()
    out = text
    for step in (drop_unused_payload_fns, fold_connects, merge_decl_assign, inline_aliases,
                 color3_rgb, collapse_blank):
        cand = step(out)
        if cand != out and parses(ast_exe, cand):
            out = cand
    with open(dst, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as fh:
        fh.write(out)
    print("polish: %d -> %d bytes" % (len(text), len(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
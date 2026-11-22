"""Luau -> Lua 5.3 source lowering for the recognized MoonVeil 2.1.x wrapper family.

Byte-level, lexer-aware and fail-closed. Runs AFTER front.normalize (binary
literals are already decimal). Lowerings:

  * compound assignment ``a op= b``  ->  ``a = a op b``
    for op in ``+ - * / // % ^ ..``.  Targets may be a name, a member chain
    (``t.a.b``) or a table index (``t[k]``, ``t[1]``, nested).  A non-name
    target with a call-bearing right-hand side is rejected instead of being
    rewritten with a duplicated side effect.
  * ``continue`` inside a loop body  ->  ``goto __mv_continue_<id>`` plus a
    matching label inserted immediately before the enclosing loop's ``end``
    (or ``until``).  A ``continue`` used like an identifier (valid Lua 5.x,
    illegal Luau) is left untouched.

Short strings, long strings and comments are single tokens, so their contents
are never modified.  Ambiguity raises ``Unsupported``; nothing is guessed.
Output preserves the original bytes everywhere except at lowered spans, so
inputs that only used the previously supported ``name += digits`` form
(``K+=1`` -> ``K=K+1``) keep byte-identical output.

Note: ``goto``/labels are accepted by the vendored Lua 5.3 parser but are NOT
implemented by the bounded pure decoder interpreter; if a *decoder helper*
ever contains ``continue``, analysis still fails closed.  Payload code is
never executed by this engine, so lowering it is safe.
"""

from __future__ import annotations

import re

from .front import Unsupported

_LONG = re.compile(rb"\[(=*)\[")
_NAME = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_NUM = re.compile(
    rb"0[xX][0-9A-Fa-f]+(?:\.[0-9A-Fa-f]*)?(?:[pP][+-]?[0-9]+)?"
    rb"|[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?"
    rb"|\.[0-9]+(?:[eE][+-]?[0-9]+)?"
)

# Multi-character operators first; single characters last.
_OPS = (
    b"...", b"..=", b"//=", b"<=", b">=", b"==", b"::", b"+=", b"-=",
    b"*=", b"/=", b"%=", b"^=", b"..", b"//", b"<", b">", b"=", b"(", b")",
    b"{", b"}", b"[", b"]", b";", b":", b",", b".", b"+", b"-", b"*", b"/",
    b"%", b"^", b"#", b"~",
)

_COMPOUND = {
    b"+=": b"+",
    b"-=": b"-",
    b"*=": b"*",
    b"/=": b"/",
    b"//=": b"//",
    b"%=": b"%",
    b"^=": b"^",
    b"..=": b"..",
}

_KEYWORDS = {
    b"and", b"break", b"do", b"else", b"elseif", b"end", b"false", b"for",
    b"function", b"goto", b"if", b"in", b"local", b"nil", b"not", b"or",
    b"repeat", b"return", b"then", b"true", b"until", b"while",
}

# Tokens that can extend an expression after a complete operand.
_CONTINUATION_OPS = {
    b"+", b"-", b"*", b"/", b"//", b"%", b"^", b"..", b"==", b"~=",
    b"<", b"<=", b">", b">=", b".", b":", b"[", b"(", b"{",
}
_CONTINUATION_KEYWORDS = {b"and", b"or"}

# Keywords that may legally start the statement after a completed `continue`.
_STMT_START_KEYWORDS = {
    b"local", b"if", b"while", b"for", b"do", b"return", b"break",
    b"repeat", b"function", b"end", b"else", b"elseif", b"until", b"goto",
}

# Tokens after which `continue` must be an identifier (Lua 5.x input), not a
# statement: assignment, member access, calls, list separators, closers.
_IDENT_FOLLOW_OPS = {b"=", b".", b":", b"(", b"[", b"{", b",", b")", b"]", b"}"}

_LOOP_KINDS = ("while", "for", "repeat")

# Lua binary operator precedence (higher binds tighter).  Used to decide
# whether a lowered right-hand side must be parenthesized: textual desugar
# `a = a op rhs` reassociates when rhs's top-level operator binds no tighter
# than `op` (e.g. `x += s..t` would otherwise become `(x+s)..t`).
_PREC = {
    b"or": 1, b"and": 2,
    b"==": 3, b"~=": 3, b"<": 3, b"<=": 3, b">": 3, b">=": 3,
    b"..": 4,
    b"+": 5, b"-": 5,
    b"*": 6, b"/": 6, b"//": 6, b"%": 6,
    b"^": 7,
}


def _tokens(src: bytes):
    """Split *src* into significant tokens (kind, start, end).

    Whitespace and comments are skipped; their bytes survive in the rebuild
    because only token spans are ever replaced.
    """
    toks = []
    i, n = 0, len(src)
    while i < n:
        c = src[i : i + 1]
        if c in b" \t\r\n\v\f":
            i += 1
            continue
        if src[i : i + 2] == b"--":
            m = _LONG.match(src, i + 2)
            if m:
                endmark = b"]" + m[1] + b"]"
                e = src.find(endmark, m.end())
                if e < 0:
                    raise Unsupported("Unterminated long comment")
                i = e + len(endmark)
            else:
                e = src.find(b"\n", i)
                i = n if e < 0 else e + 1
            continue
        if c in b"\"'":
            j = i + 1
            while j < n:
                if src[j] == 92:
                    j += 2
                elif src[j : j + 1] == c:
                    j += 1
                    break
                else:
                    j += 1
            else:
                raise Unsupported("Unterminated quoted string")
            toks.append(("str", i, j))
            i = j
            continue
        m = _LONG.match(src, i)
        if m:
            endmark = b"]" + m[1] + b"]"
            e = src.find(endmark, m.end())
            if e < 0:
                raise Unsupported("Unterminated long string")
            toks.append(("lstr", i, e + len(endmark)))
            i = e + len(endmark)
            continue
        m = _NAME.match(src, i)
        if m:
            toks.append(("name", i, m.end()))
            i = m.end()
            continue
        m = _NUM.match(src, i)
        if m and (c.isdigit() or (c == b"." and m.end() > i + 1)):
            if c == b"." and src[i : i + 2] in (b"..", b"..."):
                pass  # '..' / '...' are operators, not numbers
            else:
                toks.append(("num", i, m.end()))
                i = m.end()
                continue
        for op in _OPS:
            if src.startswith(op, i):
                toks.append(("op", i, i + len(op)))
                i += len(op)
                break
        else:
            raise Unsupported("Unexpected byte in input: %r" % src[i : i + 1])
    return toks


def _snip(b, limit=96):
    return b if len(b) <= limit else b[:limit] + b"..."


def lower(source: bytes):
    """Lower Luau compound assignments and ``continue`` statements.

    Returns ``(new_source, changes)``; *changes* is a list of dicts with
    ``kind``/``at``/``from``/``to`` (snippets truncated for reports).
    """
    if not source:
        return source, []
    toks = _tokens(source)
    text = [source[s:e] for _, s, e in toks]
    kind = [t[0] for t in toks]

    def is_name(i):
        return kind[i] == "name" and text[i] not in _KEYWORDS

    def is_op(i, t):
        return kind[i] == "op" and text[i] == t

    changes = []
    # token index -> action:
    #   {"emit": bytes, "gap": bool}  replace token bytes (emit leading gap?
    #   {"emit": None, "gap": True}   keep the leading gap, drop the token
    #   {"emit": None, "gap": False}  drop gap and token (span interior)
    actions = {}

    # ---------------- compound assignment ----------------
    def match_bracket_back(j_close):
        depth = 1
        k = j_close - 1
        while k >= 0:
            if is_op(k, b"]"):
                depth += 1
            elif is_op(k, b"["):
                depth -= 1
                if depth == 0:
                    return k
            k -= 1
        raise Unsupported("Unbalanced brackets in compound-assignment target")

    def lhs_start(ti):
        """Return (start_token_index, is_simple_name) for the compound target."""
        j = ti - 1
        if j < 0:
            raise Unsupported("Dangling compound operator")
        if not (is_name(j) or is_op(j, b"]")):
            raise Unsupported("Unsupported compound-assignment target")
        simple = is_name(j)
        first = True
        while True:
            if is_op(j, b"]"):
                k = match_bracket_back(j)
                j = k - 1
                if j < 0 or not (is_name(j) or is_op(j, b"]")):
                    raise Unsupported("Unsupported compound-assignment target")
                first = False
                simple = False
                continue
            # j is a plain name here.
            if not first:
                simple = False
            p = j - 1
            if p >= 0 and is_op(p, b"."):
                j = p - 1
                if j < 0 or not (is_name(j) or is_op(j, b"]")):
                    raise Unsupported("Malformed compound-assignment target")
                first = False
                continue
            return j, simple

    def skip_anonymous_function(j):
        """Index just past the 'end' closing a function starting at token j."""
        depth = 1
        rep = 0
        k = j + 1
        while k < len(toks):
            if kind[k] == "name":
                t = text[k]
                if t in (b"function", b"if", b"do"):
                    depth += 1
                elif t == b"end":
                    depth -= 1
                    if depth == 0:
                        return k + 1
                elif t == b"repeat":
                    rep += 1
                elif t == b"until":
                    rep -= 1
            k += 1
        raise Unsupported("Unterminated function expression")

    def rhs_span(ti):
        """Return (first_token, end_exclusive, has_calls, top_prec).

        *top_prec* is the precedence of the loosest depth-0 binary operator
        inside the right-hand side (None when it is a single operand).
        """
        j = ti + 1
        if j >= len(toks):
            raise Unsupported("Compound assignment missing right-hand side")
        state = "operand"
        depth = 0
        has_calls = False
        top_prec = None
        while j < len(toks):
            k = kind[j]
            t = text[j]
            if state == "operand":
                if k in ("num", "str", "lstr") or t in (b"true", b"false", b"nil"):
                    state = "after"
                elif is_name(j):
                    state = "after"
                elif t in (b"-", b"#", b"not"):
                    pass  # unary operator; keep expecting an operand
                elif t in (b"(", b"{", b"["):
                    depth += 1
                elif t in (b")", b"}", b"]"):
                    if depth == 0:
                        return ti + 1, j, has_calls, top_prec
                    depth -= 1
                    state = "after"
                elif t == b"function":
                    j = skip_anonymous_function(j)
                    state = "after"
                    continue
                else:
                    raise Unsupported(
                        "Malformed compound-assignment right-hand side near %r"
                        % t[:24]
                    )
            else:  # after a complete operand
                if t in (b"(", b"{", b"["):
                    if t != b"[":
                        has_calls = True
                    depth += 1
                    state = "operand"
                elif t in _CONTINUATION_OPS:
                    if depth == 0 and t in _PREC:
                        top_prec = _PREC[t] if top_prec is None else min(top_prec, _PREC[t])
                    state = "operand"
                elif k in ("str", "lstr"):
                    has_calls = True  # call sugar: f"..." / f[[...]]
                    state = "after"
                elif t in _CONTINUATION_KEYWORDS:
                    if depth == 0:
                        top_prec = _PREC[t] if top_prec is None else min(top_prec, _PREC[t])
                    state = "operand"
                else:
                    # ',', ';', a name, or any keyword ends the statement.
                    return ti + 1, j, has_calls, top_prec
            j += 1
        if state == "operand":
            raise Unsupported("Unterminated compound-assignment right-hand side")
        return ti + 1, len(toks), has_calls, top_prec

    for ti in range(len(toks)):
        if kind[ti] != "op" or text[ti] not in _COMPOUND:
            continue
        start_tok, simple = lhs_start(ti)
        first_tok, end_tok, has_calls, top_prec = rhs_span(ti)
        if not simple and has_calls:
            raise Unsupported(
                "Compound assignment on a field/index with a call-bearing "
                "right-hand side is not lowered (side-effect safety) at offset %d"
                % toks[ti][1]
            )
        lhs = source[toks[start_tok][1] : toks[ti - 1][2]]
        rhs = source[toks[first_tok][1] : toks[end_tok - 1][2]]
        op = _COMPOUND[text[ti]]
        # Parenthesize when the RHS would otherwise reassociate.
        op_prec = _PREC[op]
        if top_prec is not None and (
            top_prec < op_prec
            or (top_prec == op_prec and op not in (b"^", b".."))
        ):
            rhs = b"(" + rhs + b")"
        # `- -` would lex as a comment: keep a space between them.
        if op == b"-" and rhs.startswith(b"-"):
            rhs = b" " + rhs
        new = lhs + b"=" + lhs + op + rhs
        actions[start_tok] = {"emit": None, "gap": True}
        actions[ti] = {"emit": new, "gap": False}
        for span_idx in range(start_tok + 1, end_tok):
            if span_idx != ti:
                actions[span_idx] = {"emit": None, "gap": False}
        span_end = toks[end_tok - 1][2] if end_tok > first_tok else toks[ti][2]
        changes.append(
            {
                "kind": "compound",
                "at": toks[ti][1],
                "from": _snip(source[toks[start_tok][1] : span_end]),
                "to": _snip(new),
            }
        )

    # ---------------- continue -> goto ----------------
    stack = []  # entries: [kind, loop_id (0 = not a loop), closer_index]
    loop_id = 0
    pending = {}  # loop id -> list of `continue` token indexes
    closers = {}
    pending_header = None  # last of while/for/if/elseif seen before do/then
    for idx in range(len(toks)):
        if kind[idx] != "name":
            continue
        t = text[idx]
        if t == b"continue":
            nxt_text = text[idx + 1] if idx + 1 < len(toks) else None
            nxt_kind = kind[idx + 1] if idx + 1 < len(toks) else None
            if nxt_kind is None:
                follows_stmt = True
            elif nxt_kind == "name":
                follows_stmt = (
                    nxt_text in _STMT_START_KEYWORDS or nxt_text not in _KEYWORDS
                )
            elif nxt_kind == "op":
                if nxt_text == b";":
                    follows_stmt = True
                elif nxt_text in _IDENT_FOLLOW_OPS:
                    continue  # identifier use (Lua 5.x input): leave untouched
                else:
                    follows_stmt = False
            else:
                # A string literal follows: call sugar on an identifier.
                continue
            if not follows_stmt:
                raise Unsupported(
                    "Cannot safely lower 'continue' at offset %d" % toks[idx][1]
                )
            target = None
            for entry in reversed(stack):
                if entry[0] in _LOOP_KINDS:
                    target = entry
                    break
            if target is None:
                raise Unsupported(
                    "'continue' outside a loop at offset %d" % toks[idx][1]
                )
            pending.setdefault(target[1], []).append(idx)
            continue
        if t not in _KEYWORDS:
            continue
        if t in (b"while", b"for", b"if", b"elseif"):
            pending_header = t
        elif t == b"do":
            if pending_header in (b"while", b"for"):
                loop_id += 1
                stack.append([pending_header.decode(), loop_id, -1])
            else:
                stack.append(["do", 0, -1])
            pending_header = None
        elif t == b"then":
            if pending_header != b"elseif":
                stack.append(["if", 0, -1])
            pending_header = None
        elif t == b"function":
            stack.append(["function", 0, -1])
            pending_header = None
        elif t == b"repeat":
            loop_id += 1
            stack.append(["repeat", loop_id, -1])
            pending_header = None
        elif t in (b"end", b"until"):
            # Tolerant pop: fragments and malformed inputs surface later as
            # parse errors; this pass only needs accurate loop closers when
            # a 'continue' is actually lowered.
            if stack:
                entry = stack.pop()
                entry[2] = idx
                if entry[1]:
                    closers[entry[1]] = idx
            pending_header = None

    insertions = {}  # token index -> bytes inserted before that token
    for lid, conts in pending.items():
        if lid not in closers:
            raise Unsupported("Unbalanced loop containing 'continue'")
        label = b"::__mv_continue_%d:: " % lid
        insertions[closers[lid]] = insertions.get(closers[lid], b"") + label
        for ci in conts:
            actions[ci] = {"emit": b"goto __mv_continue_%d" % lid, "gap": True}
            changes.append(
                {
                    "kind": "continue",
                    "at": toks[ci][1],
                    "from": b"continue",
                    "to": b"goto __mv_continue_%d" % lid,
                }
            )

    # ---------------- rebuild ----------------
    out = bytearray()
    pos = 0
    for idx, (k, s, e) in enumerate(toks):
        act = actions.get(idx)
        if act is None or act["gap"]:
            out.extend(source[pos:s])
        if idx in insertions:
            out.extend(insertions[idx])
        if act is None:
            out.extend(source[s:e])
        elif act["emit"] is not None:
            out.extend(act["emit"])
        pos = e
    out.extend(source[pos:])
    changes.sort(key=lambda c: c["at"])
    return bytes(out), changes

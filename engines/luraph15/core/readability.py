"""readability.py - passes finais de LEITURA sobre o codigo levantado.

Nao muda comportamento: so comentarios, nomes e literais gigantes.

  1. assets     base64 enorme (PNG/JPEG/GZ) sai do corpo e vira arquivo:
                "iVBORw0KGgo..."  ->  "asset_01.png"  (+ pasta ao lado do .lua)
  2. aliases    `local upv110 = upv1` some; o uso passa a ser `upv1`
  3. services   `local v3 = game:GetService("RunService")` -> `RunService`
  4. rotulos    cada `local function payload_fn_N()` ganha um comentario com as
                strings que ele contem, e o arquivo ganha um indice no topo

Tudo em texto puro: o que nao casar fica exatamente como estava.
"""
import base64
import os
import re

MANY = 4                    # rodadas de inline de alias
ASSET_MIN = 300             # base64 menor que isso fica inline
HINT_MIN = 6                # tamanho minimo da string usada como pista
INDEX_MARK = "-- [readability] indice"

BASE64 = re.compile(r'"([A-Za-z0-9+/=]{%d,})"' % ASSET_MIN)
ALIAS = re.compile(r"^(\s*)local (\w+) = (\w+)\s*$")
SERVICE = re.compile(r'^(\s*)local (\w+) = (\w+):GetService\(["\'](\w+)["\']\)\s*$')
FUNC = re.compile(r"^local function (\w+)\s*\(")
LIT = re.compile(r'"([^"\n]{%d,})"' % HINT_MIN)


def _mask(text):
    """1 = codigo (fora de string), 0 = dentro de string/comentario de linha."""
    mask, quote, i, n = bytearray(len(text)), None, 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'":
            quote = c
        elif c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        else:
            mask[i] = 1
        i += 1
    return mask


def _rename(text, old, new):
    """troca o token `old` por `new` so no codigo, nunca dentro de string."""
    mask = _mask(text)
    out, i = [], 0
    for m in re.finditer(r"\b%s\b" % re.escape(old), text):
        if mask[m.start()]:
            out.append(text[i:m.start()])
            out.append(new)
            i = m.end()
    out.append(text[i:])
    return "".join(out)


def _magic(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpg"
    if raw[:2] == b"\x1f\x8b":
        return "gz"
    return "bin"


def hoist_assets(text, assets_dir):
    """base64 inline -> arquivo; devolve (texto, quantidade)."""
    made = [0]

    def sub(m):
        try:
            raw = base64.b64decode(m.group(1) + "===", validate=False)
        except Exception:
            return m.group(0)
        ext = _magic(raw)
        if len(raw) < 64 or ext == "bin":
            return m.group(0)
        made[0] += 1
        name = "asset_%02d.%s" % (made[0], ext)
        if assets_dir:
            try:
                os.makedirs(assets_dir, exist_ok=True)
                with open(os.path.join(assets_dir, name), "wb") as fh:
                    fh.write(raw)
            except OSError:
                pass
        return '"%s"' % name

    return BASE64.sub(sub, text), made[0]


def inline_aliases(text):
    """`local a = b` cujo `a` aparece uma vez depois -> usa `b` direto."""
    dropped = 0
    for _ in range(MANY):
        lines = text.split("\n")
        hit = None
        for i, line in enumerate(lines):
            m = ALIAS.match(line)
            if not m or m.group(2) == m.group(3):
                continue
            lhs = m.group(2)
            if lhs in ("self", "true", "false", "nil"):
                continue
            rest = "\n".join(lines[i + 1:])
            if len(re.findall(r"\b%s\b" % re.escape(lhs), rest)) == 1:
                hit = (i, lhs, m.group(3))
                break
        if hit is None:
            break
        i, lhs, rhs = hit
        head = "\n".join(lines[:i])
        rest = _rename("\n".join(lines[i + 1:]), lhs, rhs)
        text = (head + "\n" if head else "") + rest
        dropped += 1
    return text, dropped


def name_services(text):
    """`local X = <algo>:GetService("Svc")` -> `local Svc = ...` (X = Svc em todo o arquivo)."""
    taken = set(re.findall(r"\blocal (\w+)", text))
    renamed = 0
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = SERVICE.match(line)
        if not m:
            continue
        var, svc = m.group(2), m.group(4)
        if var == svc or svc in taken or not svc[:1].isalpha():
            continue
        text = _rename(text, var, svc)
        taken.add(svc)
        renamed += 1
        lines = text.split("\n")
    return text, renamed


def label_functions(text):
    """comentario por funcao (pistas = strings do corpo) + indice no topo."""
    lines = text.split("\n")
    comments, index = [], []
    for i, line in enumerate(lines):
        m = FUNC.match(line)
        if not m:
            continue
        body, j = [], i + 1
        while j < len(lines) and not FUNC.match(lines[j]) and lines[j] != "end":
            body.append(lines[j])
            j += 1
        hints = []
        for h in LIT.findall("\n".join(body)):
            if h.startswith("devirt:") or h.startswith("asset_") or h in hints:
                continue
            hints.append(h)
            if len(hints) == 2:
                break
        name = m.group(1)
        if hints:
            ind = re.match(r"^\s*", line).group(0)
            comments.append((i, '%s-- %s <- %s' % (ind, name, ", ".join('"%s"' % h[:60] for h in hints))))
            index.append("--   %-22s %-7s %s" % (name, "L%d" % (i + 1), hints[0][:60]))
        else:
            index.append("--   %-22s %-7s -" % (name, "L%d" % (i + 1)))
    if not index:
        return text, 0
    for i, comment in reversed(comments):
        lines.insert(i, comment)
    if INDEX_MARK not in text:
        head = [INDEX_MARK + " (%d funcoes)" % len(index)] + index + ["-- ]]"]
        k = 0
        while k < len(lines) and lines[k].startswith("--"):
            k += 1
        lines[k:k] = head
    return "\n".join(lines), len(comments)


def improve(text, assets_dir=None):
    text, assets = hoist_assets(text, assets_dir)
    text, aliases = inline_aliases(text)
    text, services = name_services(text)
    text, labeled = label_functions(text)
    return text, {"assets": assets, "aliases": aliases,
                  "services": services, "labeled": labeled}


def describe(stats):
    return ("%d asset(s) fora do corpo, %d alias(es) inlinado(s), "
            "%d nome(s) de servico, %d funcao(oes) rotulada(s)" % (
                stats["assets"], stats["aliases"], stats["services"], stats["labeled"]))

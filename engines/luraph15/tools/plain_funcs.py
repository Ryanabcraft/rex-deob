"""List the functions Luraph left NON-virtualized (LPH_NO_VIRTUALIZE /
LPH_JIT): they sit in plain Luau inside the VM object table as
`[key]=function(...) return function(<upvalue pack>, ...) <real code> end end`.

Useful when the VM cannot run offline (stage-2 chunks called as
`setmetatable({...},{}):X(...)` that expect their bytecode from the caller,
key-system / auth-locked loaders): this code is readable without running.

usage: python tools/plain_funcs.py <luau-ast> <in.lua> [out.lua]
"""
import json, os, subprocess, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
from lexer import tokenize, pretty   # noqa: E402


def _pos(loc):
    a, b = loc.split(" - ")
    return tuple(int(x) for x in a.split(",")), tuple(int(x) for x in b.split(","))


def extract(ast_exe, path):
    """Low-memory: the AST JSON of a 1MB Luraph file is ~50MB and used to
    become ~550MB of Python dicts. Nodes are compacted while the JSON is
    parsed (object_hook runs bottom-up): every function keeps only its
    location, the location of the function it returns, and the outermost
    tables inside it; every table keeps only its candidate entries. Peak
    memory is then a small multiple of the JSON text instead of ~10x."""
    src = open(path, "rb").read()
    proc = subprocess.run([ast_exe, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode:
        return []
    raw = proc.stdout.decode("utf-8", "replace")   # string constants can hold invalid UTF-8
    del proc

    def outer_tables(n):
        if isinstance(n, dict):
            if n.get("type") == "AstExprTable":
                yield n
                return              # the VM object table is never nested
            for v in n.values():
                yield from outer_tables(v)
        elif isinstance(n, list):
            for v in n:
                yield from outer_tables(v)

    def hook(d):
        t = d.get("type")
        if t == "AstExprFunction":
            inner = None
            stats = (d.get("body") or {}).get("body") or []
            if len(stats) == 1 and stats[0].get("type") == "AstStatReturn":
                lst = stats[0].get("list") or []
                if len(lst) == 1 and lst[0].get("type") == "AstExprFunction":
                    inner = lst[0].get("location")
            return {"type": t, "location": d.get("location"), "_inner": inner,
                    "_sub": list(outer_tables(d.get("body")))}
        if t == "AstExprTable":
            found = []
            for it in d.get("items") or []:
                key, val = it.get("key"), it.get("value")
                if not key or key.get("type") != "AstExprConstantNumber" or not val \
                        or val.get("type") != "AstExprFunction" or not val.get("_inner"):
                    continue
                found.append((key.get("value"), val["_inner"]))
            return {"type": t, "_found": found}
        return d

    root = json.loads(raw, object_hook=hook)["root"]
    del raw
    starts = [0]
    i = src.find(b"\n")
    while i >= 0:
        starts.append(i + 1)
        i = src.find(b"\n", i + 1)

    def text(loc):
        (l0, c0), (l1, c1) = _pos(loc)
        return src[starts[l0] + c0:starts[l1] + c1].decode("latin-1")

    found = []
    for t in outer_tables(root):
        for k, loc in t["_found"]:
            found.append((k, text(loc)))
    return found


def render(found):
    out = ["-- %d non-virtualized function(s) found in plain Luau inside the VM table." % len(found),
           "-- Each is a factory: its 1st parameter is the upvalue pack (`local a = B[1]` ...),",
           "-- the function it returns is the real code.", ""]
    for k, code in found:
        try:
            body = pretty(tokenize(code))
        except Exception:
            body = code
        out.append("-- [%s]" % k)
        out.append("local plain_%s = %s" % (str(k).replace(".", "_").replace("-", "m"), body))
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    found = extract(sys.argv[1], sys.argv[2])
    res = render(found) if found else ""
    if len(sys.argv) > 3:
        open(sys.argv[3], "w").write(res)
    else:
        try:
            sys.stdout.write(res)
        except BrokenPipeError:
            pass

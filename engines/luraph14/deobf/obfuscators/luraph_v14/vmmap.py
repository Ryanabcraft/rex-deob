"""
Static helper: maps every Luraph v14.x interpreter dispatch loop to its opcode
handlers (opcode -> handler source) using luau-ast's JSON output.

v14 shape (differs from v15):
    while true do
      local INSTR = (ARR[PC]);     -- instruction fetch (may be parenthesized)
      local OP = (INSTR[6]);       -- opcode = instruction field 6
      PC = (PC + 1);               -- pc increment (assign or compound)
      if-tree on OP                -- binary search; conditions may be `not(...)`
    end

Usage: python obfuscators/luraph_v14/vmmap.py <protected.luau> [dispatch_index] [opcode...]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))     # deobf/ (bin/luau-ast)


def load_ast(path):
    exe = os.path.join(ROOT, "bin", "luau-ast.exe" if os.name == "nt" else "luau-ast")
    r = subprocess.run([exe, path], capture_output=True)
    errs = r.stderr.decode("latin-1").strip().splitlines()
    if r.returncode != 0 or (errs and errs[0].startswith("Parse errors")):
        raise SyntaxError("not valid Luau (%s)" % (errs[1].strip() if len(errs) > 1 else "luau-ast exit %#x"
                                                   % (r.returncode & 0xFFFFFFFF)))
    out = r.stdout
    return json.loads(out.decode("latin-1"))["root"]


def loc(node):
    a, b = node["location"].split(" - ")
    l1, c1 = map(int, a.split(","))
    l2, c2 = map(int, b.split(","))
    return l1, c1, l2, c2


def text_of(lines, node):
    l1, c1, l2, c2 = loc(node)
    if l1 == l2:
        return lines[l1][c1:c2]
    parts = [lines[l1][c1:]] + lines[l1 + 1:l2] + [lines[l2][:c2]]
    return "\n".join(parts)


def walk(node, fn):
    if isinstance(node, dict):
        fn(node)
        for v in node.values():
            walk(v, fn)
    elif isinstance(node, list):
        for v in node:
            walk(v, fn)


def local_name(expr):
    if isinstance(expr, dict) and expr.get("type") == "AstExprLocal":
        return expr["local"]["name"]
    return None


def ungroup(e):
    """luau-ast JSON paren groups: AstExprGroup{expr=...} (this build) or
    {expression=...}; strip all of them."""
    while isinstance(e, dict) and e.get("type") == "AstExprGroup":
        e = e.get("expr") if "expr" in e else e.get("expression")
        if e is None:
            return None
    return e


def visit_iter(root, visit):
    """Iterative full walk (the AST of a 2 MB script overflows the Python
    stack with a recursive walker)."""
    todo = [root]
    while todo:
        n = todo.pop()
        visit(n)
        if isinstance(n, dict):
            for v in n.values():
                if isinstance(v, (dict, list)):
                    todo.append(v)
        elif isinstance(n, list):
            for v in n:
                if isinstance(v, (dict, list)):
                    todo.append(v)


def find_dispatchers(root):
    """v14: while true do local INSTR=(ARR[PC]); local OP=(INSTR[6]);
    PC=(PC+1); if-tree ... end. The if-tree may sit at index 1 (v15 shape,
    kept for compatibility) or index 3 (v14 shape)."""
    found = []

    def visit(n):
        if not isinstance(n, dict) or n.get("type") != "AstStatWhile":
            return
        cond = n["condition"]
        if cond.get("type") != "AstExprConstantBool" or not cond.get("value"):
            return
        body = n["body"]["body"]
        if len(body) < 2 or body[0]["type"] not in ("AstStatLocal", "AstStatAssign"):
            return
        st = body[0]
        if len(st.get("values", [])) != 1:
            return
        if st["type"] == "AstStatLocal":
            opname = st["vars"][0]["name"]
        else:
            opname = local_name(st["vars"][0])
            if not opname:
                return
        v = ungroup(st["values"][0])
        if v is None or v.get("type") != "AstExprIndexExpr":
            return
        arr, pc = local_name(v.get("expr")), local_name(v.get("index"))
        if not arr or not pc:
            return
        # the opcode-dispatch if-tree: directly after the fetch (v15 shape),
        # or after an opcode fetch + pc increment (v14 shape). v14's tree
        # tests the opcode variable (body[1]'s target), not the instruction.
        idx = 1
        opvar = opname
        if idx < len(body) and body[idx]["type"] == "AstStatLocal" and len(body[idx].get("values", [])) == 1:
            ov = ungroup(body[idx]["values"][0])
            if ov is not None and ov.get("type") == "AstExprIndexExpr" \
                    and local_name(ov.get("expr")) == opname:
                opvar = body[idx]["vars"][0]["name"]
                idx += 1
        if idx < len(body) and body[idx]["type"] in ("AstStatAssign", "AstStatCompoundAssign"):
            tgt = body[idx].get("vars", [None])[0] if "vars" in body[idx] else body[idx].get("var")
            if local_name(tgt) == pc:
                idx += 1
        if idx >= len(body) or body[idx]["type"] != "AstStatIf":
            return
        found.append({"node": n, "op": opvar, "arr": arr, "pc": pc, "tree": body[idx],
                      "rest": body[idx + 1:]})

    visit_iter(root, visit)
    return found


OPS = {"CompareLt": lambda a, b: a < b, "CompareLe": lambda a, b: a <= b,
       "CompareGt": lambda a, b: a > b, "CompareGe": lambda a, b: a >= b,
       "CompareEq": lambda a, b: a == b, "CompareNe": lambda a, b: a != b}
FLIP = {"CompareLt": "CompareGt", "CompareLe": "CompareGe", "CompareGt": "CompareLt",
        "CompareGe": "CompareLe", "CompareEq": "CompareEq", "CompareNe": "CompareNe"}


def eval_cond(cond, var, value):
    """Evaluate `var <op> const` for a concrete opcode value; None if not such
    a test. Handles paren groups and v14's `not(...)` wrappers."""
    cond = ungroup(cond)
    if cond is None:
        return None
    if cond.get("type") == "AstExprUnary" and cond.get("op") == "Not":
        raw = cond.get("expression") if "expression" in cond else cond.get("expr")
        inner = eval_cond(ungroup(raw), var, value)
        return None if inner is None else not inner
    if cond["type"] != "AstExprBinary" or cond["op"] not in OPS:
        return None
    l, r = ungroup(cond["left"]), ungroup(cond["right"])
    op = cond["op"]
    if local_name(l) == var and r is not None and r.get("type") == "AstExprConstantNumber":
        return OPS[op](value, r["value"])
    if local_name(r) == var and l is not None and l.get("type") == "AstExprConstantNumber":
        return OPS[FLIP[op]](value, l["value"])
    return None


def resolve(tree, var, value):
    """Follow the if-tree for one opcode value and return the handler block."""
    node = tree
    while True:
        if node["type"] == "AstStatBlock":
            if len(node["body"]) >= 1 and node["body"][0]["type"] == "AstStatIf" \
                    and eval_cond(node["body"][0]["condition"], var, value) is not None:
                node = node["body"][0]
                continue
            return node
        if node["type"] == "AstStatIf":
            r = eval_cond(node["condition"], var, value)
            if r is None:
                return node
            if r:
                node = node["thenbody"]
            else:
                e = node.get("elsebody")
                if e is None:
                    return None
                node = e
            continue
        return node


def handler_map(disp, lines, max_op=256):
    handlers = {}
    for op in range(max_op):
        blk = resolve(disp["tree"], disp["op"], op)
        if blk is None:
            continue
        key = blk["location"]
        handlers.setdefault(key, {"ops": [], "node": blk})["ops"].append(op)
    return handlers


def main():
    path = sys.argv[1]
    root = load_ast(path)
    with open(path, encoding="latin-1") as f:
        lines = f.read().split("\n")
    disps = find_dispatchers(root)
    if len(sys.argv) == 2:
        for i, d in enumerate(disps, 1):
            h = handler_map(d, lines)
            print("dispatch %d: op=%s arr=%s pc=%s handlers=%d at %s" % (i, d["op"], d["arr"], d["pc"], len(h),
                                                                        d["node"]["location"]))
        return
    d = disps[int(sys.argv[2]) - 1]
    want = set(int(x) for x in sys.argv[3:])
    for op in sorted(want):
        blk = resolve(d["tree"], d["op"], op)
        print("---- op %d" % op)
        print(text_of(lines, blk) if blk else "<none>")


if __name__ == "__main__":
    main()


def instrument(src, disp_index, probes, root=None, tmp_path=None):
    """Insert Lua code at the start of chosen opcode handlers of one dispatch loop.

    probes: {opcode: "lua code"}; returns modified source. Only single-line
    sources (like Luraph output) are supported.
    """
    lines = src.split("\n")
    if root is None:
        root = load_ast(tmp_path)
    d = find_dispatchers(root)[disp_index - 1]
    inserts = []
    for op, code in probes.items():
        blk = resolve(d["tree"], d["op"], op)
        if blk is None:
            continue
        l1, c1, _, _ = loc(blk)
        # block location starts right at the handler's first statement
        inserts.append((l1, c1, code))
    for l1, c1, code in sorted(inserts, reverse=True):
        s = lines[l1]
        lines[l1] = s[:c1] + " " + code + " " + s[c1:]
    return "\n".join(lines)


def instrument_post(src, disp_index, root, make_code, reg="Z", pc="W"):
    """Append logging after simple `REG[..]=...` handlers. make_code(op, dest_expr) -> lua."""
    import re as _re
    lines = src.split("\n")
    d = find_dispatchers(root)[disp_index - 1]
    h = handler_map(d, lines)
    inserts = []
    for key, v in h.items():
        blk = v["node"]
        text = text_of(lines, blk)
        if (pc + "=") in text.replace(pc + "==", "") or "return" in text or "break" in text \
                or (pc + "+=") in text or (pc + "-=") in text:
            continue
        m = _re.match(r"\s*(" + reg + r"\[[A-Za-z_]+\[" + pc + r"\]\])=", text)
        if not m:
            continue
        l1, c1, l2, c2 = loc(blk)
        inserts.append((l2, c2, make_code(v["ops"][0], m.group(1))))
    for l2, c2, code in sorted(inserts, reverse=True):
        s = lines[l2]
        lines[l2] = s[:c2] + " " + code + " " + s[c2:]
    return "\n".join(lines)


def loop_names(disp):
    """(register array, pc variable) used by a dispatch loop's handlers."""
    return ("Z", "W") if disp["pc"] == "W" else ("l", "O")


def post_inserts(src_lines, disp, make_code):
    import re as _re
    reg, pc = loop_names(disp)
    out = []
    for v in handler_map(disp, src_lines).values():
        text = text_of(src_lines, v["node"])
        if (pc + "=") in text.replace(pc + "==", "") or "return" in text or "break" in text \
                or (pc + "+=") in text or (pc + "-=") in text:
            continue
        m = _re.match(r"\s*(" + reg + r"\[[A-Za-z_]+\[" + pc + r"\]\])=", text)
        if not m:
            continue
        l1, c1, l2, c2 = loc(v["node"])
        out.append((l2, c2, make_code(v["ops"][0], m.group(1), pc)))
    return out


def instrument_everything(src, root, make_post, make_loop):
    """Post-log simple handlers in every loop and log each dispatched instruction."""
    import re as _re
    lines = src.split("\n")
    inserts = []
    for d in find_dispatchers(root):
        inserts += post_inserts(lines, d, make_post)
    for l2, c2, code in sorted(inserts, reverse=True):
        s = lines[l2]
        lines[l2] = s[:c2] + " " + code + " " + s[c2:]
    out = "\n".join(lines)
    k = [0]

    def rep(m):
        k[0] += 1
        return m.group(0) + make_loop(k[0], m.group(1), m.group(3))
    return _re.sub(r"while true do (?:local )?([A-Za-z_]+)(?:,[A-Za-z_]+)*=([A-Za-z_]+)\[([A-Za-z_]+)\];", rep, out)


def closure_entries(root):
    """For each VM interpreter closure: (line, col of its first statement, proto variable name)."""
    disp_nodes = [d["node"] for d in find_dispatchers(root)]
    seen = {}

    def walk(n, stack):
        if isinstance(n, dict):
            if n.get("type") == "AstExprFunction":
                stack = stack + [n]
            if n.get("type") == "AstStatWhile" and any(n is d for d in disp_nodes):
                inner = next(f for f in reversed(stack) if f.get("vararg") and not f["args"])
                outer = next(f for f in reversed(stack) if len(f["args"]) >= 2)
                l1, c1, _, _ = loc(inner["body"]["body"][0])
                # keyed by the proto parameter: parameter 1 is not always the
                # proto (function(g, upvals, proto, ...)), and an upvalue list
                # can be shared or false, which would merge protos
                seen[(l1, c1)] = outer["args"][_maker_params(outer)[0]]["name"]
            for v in n.values():
                walk(v, stack)
        elif isinstance(n, list):
            for v in n:
                walk(v, stack)
    walk(root, [])
    return [(l, c, name) for (l, c), name in seen.items()]


def closure_makers(root):
    """For each VM closure template: (line, col just after the statement that
    stores the new closure in a local, that local's name, proto variable name).
    v14 builds every closure of a proto with `_ = function(...) ... end` inside
    a maker function whose first parameter is the proto; the VM closure may be
    nested below the maker through plain wrapper functions."""
    disp_nodes = [d["node"] for d in find_dispatchers(root)]
    seen = {}

    def walk(n, stack, stmts):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "AstExprFunction":
                stack = stack + [n]
            if t and t.startswith("AstStat"):
                stmts = stmts + [(n, len(stack))]
            if t == "AstStatWhile" and any(n is d for d in disp_nodes):
                vi = next((i for i, f in reversed(list(enumerate(stack)))
                           if f.get("vararg") and not f["args"]), None)
                oi = next((i for i, f in reversed(list(enumerate(stack[:vi])))
                           if len(f["args"]) >= 2), None)
                if vi is not None and oi is not None:
                    clo = stack[vi]
                    st = [s for s, d in stmts if d == vi]
                    st = st[-1] if st else None
                    if st and st["type"] == "AstStatAssign":
                        for v, e in zip(st["vars"], st["values"]):
                            e = ungroup(e)
                            if e is clo and local_name(v):
                                _, _, l2, c2 = loc(st)
                                seen[(l2, c2)] = (local_name(v), stack[oi]["args"][_maker_params(stack[oi])[0]]["name"])
            for v in n.values():
                walk(v, stack, stmts)
        elif isinstance(n, list):
            for v in n:
                walk(v, stack, stmts)
    walk(root, [], [])
    return [(l, c, var, pv) for (l, c), (var, pv) in seen.items()]


def decl_key(local):
    """Identity of a local: its declaration location."""
    return local["location"]


def _iter_local_decls(n, out):
    """Collect every local declaration (decl location -> name) in a subtree."""
    if isinstance(n, dict):
        t = n.get("type")
        if t == "AstStatLocal":
            for v in n.get("vars", []):
                out[v["location"]] = v["name"]
        elif t == "AstStatLocalFunction":
            out[n["name"]["location"]] = n["name"]["name"]
        elif t == "AstStatFor" and "var" in n:
            out[n["var"]["location"]] = n["var"]["name"]
        elif t == "AstStatForIn":
            for v in n.get("vars", []):
                out[v["location"]] = v["name"]
        elif t == "AstExprFunction":
            for a in n.get("args", []):
                out[a["location"]] = a["name"]
        for v in n.values():
            _iter_local_decls(v, out)
    elif isinstance(n, list):
        for v in n:
            _iter_local_decls(v, out)


def _iter_local_uses(n, out):
    """Collect the decl locations of every local reference in a subtree."""
    if isinstance(n, dict):
        if n.get("type") == "AstExprLocal":
            out.add(n["local"]["location"])
        for v in n.values():
            _iter_local_uses(v, out)
    elif isinstance(n, list):
        for v in n:
            _iter_local_uses(v, out)


def maker_upvalues(root, maker):
    """{decl_location: name} of locals declared OUTSIDE `maker` that its body
    (including the VM closures it builds) references -- the maker's upvalues,
    plus the TRANSITIVE upvalues of the main-level functions among them (a
    captured helper's own upvalues are needed to run its source).
    luau-ast resolves every identifier to its declaration, so the decl
    locations are exact; values come from the __SNAP statements the driver
    inserts (main_snapshot_ids) or from the maker capture, by name."""
    all_decls = {}
    _iter_local_decls(root, all_decls)
    inner = {}
    _iter_local_decls(maker, inner)
    fn_nodes = {}
    _iter_func_decls(root, fn_nodes)

    def uses_of(node):
        used = set()
        _iter_local_uses(node, used)
        return used

    ups = {}
    todo = [(used, inner) for used in [uses_of(maker)]]
    seen_fns = set()
    while todo:
        used, inner_decls = todo.pop()
        for loc in used:
            if loc in inner_decls:
                continue
            nm = all_decls.get(loc)
            if nm is None:
                continue
            if loc not in ups:
                ups[loc] = nm
            fn = fn_nodes.get(loc)
            if fn is not None and id(fn) not in seen_fns:
                seen_fns.add(id(fn))
                fn_inner = {}
                _iter_local_decls(fn, fn_inner)
                todo.append((uses_of(fn), fn_inner))
    return ups


def _enclosing_main(root, maker):
    """The outermost AstExprFunction whose span contains `maker` (main)."""
    best = None
    ml = loc(maker)
    stack = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("type") == "AstExprFunction" and n is not maker:
                sp = loc(n)
                if sp[0:2] <= ml[0:2] and ml[2:4] <= sp[2:4]:
                    if best is None or sp[0:2] <= loc(best)[0:2]:
                        best = n
            for v in n.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(n, list):
            stack.extend(v for v in n if isinstance(v, (dict, list)))
    return best


def main_snapshots(root, makers):
    """(snap_ids, insertions): the main function's top-level locals and
    params are captured after every top-level statement (`if __SNAP then
    __SNAP[id]=name end`, table operations only), so the lifter sees their
    FINAL values even when a name is declared twice (the second declaration
    shadows the first: its last visible snapshot is the one kept).
    snap_ids: {decl_location: id} of each local's last visible snapshot;
    insertions: [(line, col, code)] for the driver to splice in."""
    maker0 = makers[0]
    mainfn = _enclosing_main(root, maker0)
    if mainfn is None:
        return {}, []
    snap = {}
    ins = []
    nid = [0]

    def emit(after_l, after_c, pairs):
        if not pairs:
            return
        code = "if __SNAP then "
        for nm, loc_ in pairs:
            nid[0] += 1
            snap.setdefault(loc_, []).append(nid[0])
            code += "__SNAP[%d]=%s;" % (nid[0], nm)
        ins.append((after_l, after_c, code + "end "))

    body = mainfn["body"]["body"]
    if not body:
        return snap, ins
    visible = [(a["name"], a["location"]) for a in mainfn["args"]]
    # parameters: one snapshot before the first statement
    l1, c1, _, _ = loc(body[0])
    emit(l1, c1, visible)
    for st in body:
        # new declarations shadow older ones from this statement on
        decls = []
        if st.get("type") == "AstStatLocal":
            decls = [(v["name"], v["location"]) for v in st.get("vars", [])]
        elif st.get("type") == "AstStatLocalFunction":
            decls = [(st["name"]["name"], st["name"]["location"])]
        if decls:
            # a redeclaration shadows the old mapping for that name
            names = {nm for nm, _ in decls}
            visible = [(nm, lc) for nm, lc in visible if nm not in names]
            visible += decls
        l2, c2 = loc(st)[2:4]
        emit(l2, c2, visible)
    return snap, ins


def _iter_func_decls(n, out):
    """decl location -> AstExprFunction for `local f = function...` and
    later `f = function...` assignments."""
    if isinstance(n, dict):
        t = n.get("type")
        if t == "AstStatLocal":
            vals = n.get("values", [])
            for i, v in enumerate(n.get("vars", [])):
                if i < len(vals):
                    e = ungroup(vals[i])
                    if e is not None and e.get("type") == "AstExprFunction":
                        out[v["location"]] = e
        elif t == "AstStatAssign":
            for v, e in zip(n.get("vars", []), n.get("values", [])):
                e = ungroup(e)
                if e is not None and e.get("type") == "AstExprFunction" \
                        and v.get("type") == "AstExprLocal":
                    out[v["local"]["location"]] = e
        for v in n.values():
            _iter_func_decls(v, out)
    elif isinstance(n, list):
        for v in n:
            _iter_func_decls(v, out)


def maker_info(root):
    """closure_makers() plus what the runtime capture needs: for each maker,
    {"at": (line, col), "var": closure local, "proto": proto param name,
     "maker": maker AstExprFunction, "vm": the VM closure AstExprFunction,
     "captures": [maker-level local names the VM closure uses]}.
    Only names that are not shadowed at the insertion point are returned."""
    disp_nodes = [d["node"] for d in find_dispatchers(root)]
    out = {}

    def walk(n, stack, stmts):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "AstExprFunction":
                stack = stack + [n]
            if t and t.startswith("AstStat"):
                stmts = stmts + [(n, len(stack))]
            if t == "AstStatWhile" and any(n is d for d in disp_nodes):
                vi = next((i for i, f in reversed(list(enumerate(stack)))
                           if f.get("vararg") and not f["args"]), None)
                oi = next((i for i, f in reversed(list(enumerate(stack[:vi])))
                           if len(f["args"]) >= 2), None)
                if vi is not None and oi is not None:
                    clo = stack[vi]
                    st = [s for s, d in stmts if d == vi]
                    st = st[-1] if st else None
                    if st and st["type"] == "AstStatAssign":
                        for v, e in zip(st["vars"], st["values"]):
                            e = ungroup(e)
                            if e is clo and local_name(v):
                                _, _, l2, c2 = loc(st)
                                if (l2, c2) not in out:
                                    pi, ui = _maker_params(stack[oi])
                                    out[(l2, c2)] = {"at": (l2, c2), "var": local_name(v),
                                                     "proto": stack[oi]["args"][pi]["name"],
                                                     "proto_index": pi, "upvals_index": ui,
                                                     # key the entry hooks number protos by (__PID);
                                                     # closure -> proto attribution must use the same
                                                     "pf_key": stack[oi]["args"][pi]["name"],
                                                     "maker": stack[oi], "vm": clo, "stmt": st,
                                                     # main-level locals the maker (and its VM
                                                     # closures) reference: captured by name at
                                                     # runtime, bound by the lifter as the maker's
                                                     # upvalue scope (v14 has no VM object)
                                                     "upvals": maker_upvalues(root, stack[oi])}
            for v in n.values():
                walk(v, stack, stmts)
        elif isinstance(n, list):
            for v in n:
                walk(v, stack, stmts)
    walk(root, [], [])
    for info in out.values():
        info["captures"] = _captures(info)
    return list(out.values())


def _maker_params(maker):
    """(proto parameter index, upvalue-list parameter index) of a closure maker.
    v14 layouts: function(proto, upvals) -- the proto is parameter 0 -- or,
    with repeated names, function(g, d, d, d, d, j) where the proto is the
    last one. The proto is the parameter indexed with constant keys the most
    (P[1], P[3], ...: its fields hold the proto's arrays) and/or indexed
    through itself (P[P[k]]); the upvalue list is the first other parameter
    the body uses (only the last of repeated names is visible)."""
    args = maker["args"]
    visible = {}
    for i, a in enumerate(args):
        visible[a["name"]] = i
    counts = {}
    kcounts = {}    # P[<number>]: the proto of makers that index it directly
    used = set()

    def visit(n, depth):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "AstExprFunction":
                return      # nested closures' h[x] are instruction reads, not proto reads
            if t == "AstExprLocal":
                used.add(n["local"]["location"])
            if t == "AstExprIndexExpr" and n["expr"].get("type") == "AstExprLocal" \
                    and n["index"].get("type") == "AstExprIndexExpr" \
                    and n["index"]["expr"].get("type") == "AstExprLocal" \
                    and n["index"]["expr"]["local"]["location"] == n["expr"]["local"]["location"]:
                k = n["expr"]["local"]["location"]
                counts[k] = counts.get(k, 0) + 1
            if t == "AstExprIndexExpr" and n["expr"].get("type") == "AstExprLocal" \
                    and n["index"].get("type") == "AstExprConstantNumber":
                k = n["expr"]["local"]["location"]
                kcounts[k] = kcounts.get(k, 0) + 1
            for v in n.values():
                visit(v, depth)
        elif isinstance(n, list):
            for v in n:
                visit(v, depth)
    visit(maker["body"], 0)
    cands = list(visible.values())
    # v14: the proto is usually parameter 0, read with constant keys directly
    # in the maker body (h[1], h[3], ...); constant-key indexes INSIDE nested
    # VM closures are instruction-field reads of shadowing locals, so only the
    # v15-style self-index count is computed over the whole body
    pi = max(cands, key=lambda i: (counts.get(args[i]["location"], 0),
                                   kcounts.get(args[i]["location"], 0), -i)) if cands else 0
    if not counts.get(args[pi]["location"]) and not kcounts.get(args[pi]["location"]):
        pi = 0
        # function(v, R, R, R, g, g) with `B = R[11]`: the visible R
        kc = [i for i in cands if kcounts.get(args[i]["location"])]
        if kc:
            pi = max(kc, key=lambda i: kcounts[args[i]["location"]])
    others = sorted(i for i in visible.values() if i != pi and args[i]["location"] in used)
    ui = others[0] if others else (pi + 1)
    return pi, ui


def _decls_in(fn):
    """Declaration keys of locals declared directly in fn (params and body,
    not inside nested functions)."""
    keys = {}

    def visit(n):
        if isinstance(n, dict):
            t = n.get("type")
            if t == "AstExprFunction" and n is not fn:
                return
            if t == "AstStatLocal":
                for v in n["vars"]:
                    keys[decl_key(v)] = v["name"]
            elif t == "AstStatLocalFunction":
                keys[decl_key(n["name"])] = n["name"]["name"]
            elif t in ("AstStatFor",):
                keys[decl_key(n["var"])] = n["var"]["name"]
            elif t == "AstStatForIn":
                for v in n["vars"]:
                    keys[decl_key(v)] = v["name"]
            for v in n.values():
                visit(v)
        elif isinstance(n, list):
            for v in n:
                visit(v)
    for a in fn["args"]:
        keys[decl_key(a)] = a["name"]
    visit(fn["body"])
    return keys


def _captures(info):
    maker_decls = _decls_in(info["maker"])
    used = {}

    def visit(n):
        if isinstance(n, dict):
            if n.get("type") == "AstExprLocal":
                k = decl_key(n["local"])
                if k in maker_decls:
                    used[k] = maker_decls[k]
            for v in n.values():
                visit(v)
        elif isinstance(n, list):
            for v in n:
                visit(v)
    visit(info["vm"])
    names = {}
    for k, name in used.items():
        names.setdefault(name, []).append(k)
    # a name declared twice at maker level would be ambiguous at the insertion point
    dup = {nm for nm, ks in names.items() if len(ks) > 1}
    by_name = {}
    for k, nm in maker_decls.items():
        by_name.setdefault(nm, []).append(k)
    return sorted(nm for nm in names if nm not in dup and len(by_name[nm]) == 1)

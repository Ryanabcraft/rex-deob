import hashlib

from .lua.parser import Node
from .lua.runtime import LuaTable


def canon(node, names):
    if isinstance(node, Node):
        if node.t == "name":
            return "N%d" % names.setdefault(node.name, len(names))
        parts = [node.t]
        for key in sorted(vars(node)):
            if key in ("t", "raw", "binding") or (key == "name" and node.t == "func"):
                continue
            parts.append("%s=%s" % (key, canon(getattr(node, key), names)))
        return "(" + " ".join(parts) + ")"
    if isinstance(node, (list, tuple)):
        return "[" + ",".join(canon(x, names) for x in node) + "]"
    return repr(node)


def fingerprint(func_node):
    return hashlib.sha1(canon(func_node, {}).encode("utf-8")).hexdigest()


KNOWN = {
    "9653ea97ddc7d000840e2df2af99aeb85bd9356d": "rc4",
    "b9aecb1210cfc70c27c938092fb3211bebd76f90": "xor",
    "d7ed8139a1ec62d7aa90cd93f47b1c9c37802b70": "chars",
}


def rc4_native(data, key):
    if data.__class__ is not bytes or key.__class__ is not LuaTable or key.n == 0:
        return None
    n = key.n
    kd = key.d
    kl = [kd.get(i) for i in range(1, n + 1)]
    for k in kl:
        if k.__class__ is not int:
            return None
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + kl[i % n]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray(len(data))
    for idx, b in enumerate(data):
        i = (i + 1) & 255
        si = s[i]
        j = (j + si) & 255
        sj = s[j]
        s[i] = sj
        s[j] = si
        out[idx] = (b ^ s[(si + sj) & 255]) & 0xFFFFFFFF
    return [bytes(out)]


def xor_native(data, key):
    if data.__class__ is not bytes or key.__class__ is not LuaTable or key.n == 0:
        return None
    n = key.n
    kd = key.d
    kl = [kd.get(i) for i in range(1, n + 1)]
    for k in kl:
        if k.__class__ is not int or not 0 <= k <= 255:
            return None
    if n == 1:
        k0 = kl[0]
        return [bytes(b ^ k0 for b in data)]
    reps = -(-len(data) // n)
    stream = (bytes(kl) * reps)[:len(data)]
    return [bytes(a ^ b for a, b in zip(data, stream))]


def helper_native(table):
    if table.__class__ is not LuaTable:
        return None
    d = table.d
    vals = [d.get(i) for i in range(1, table.n + 1)]
    for v in vals:
        if v.__class__ is not int or not 0 <= v <= 255:
            return None
    return [bytes(vals)]


NATIVE = {"rc4": rc4_native, "xor": xor_native, "chars": helper_native}
NEEDS_BXOR = {"rc4": True, "xor": True, "chars": False}


class FastPaths:
    def __init__(self, genv):
        d = genv.d
        self.bxor = d[b"bit32"].d[b"bxor"]
        self.libs = [d[b"string"], d[b"math"], d[b"table"]]

    def trusted(self, upvalues, kind):
        seen_bxor = False
        for cell in upvalues:
            v = cell[0]
            if v is self.bxor:
                seen_bxor = True
            elif not any(v is lib for lib in self.libs):
                return False
        return seen_bxor or not NEEDS_BXOR[kind]

    def wrap(self, func_node, make):
        kind = KNOWN.get(fingerprint(func_node))
        if kind is None:
            return make
        impl = NATIVE[kind]
        owner = self

        def mk(fr):
            interpreted = make(fr)
            if not owner.trusted(interpreted.up, kind):
                return interpreted

            def fast(*args):
                r = impl(*args)
                if r is None:
                    return interpreted(*args)
                return r
            return fast
        return mk

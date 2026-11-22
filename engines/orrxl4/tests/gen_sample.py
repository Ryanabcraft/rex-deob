import random
import re

HEADER = "-- PROTECTED BY ORRXL4 OBFUSCATOR\n"
ALNUM = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def rc4(data, key):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for b in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def xor_cycle(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


class Names:
    def __init__(self, rng):
        self.rng = rng
        self.used = set()

    def new(self):
        while True:
            n = "_" + "".join(self.rng.choice("IlO01") for _ in range(self.rng.randint(11, 14)))
            if n not in self.used:
                self.used.add(n)
                return n


RC4_SRC = """local function {RC4}({d},{k})
local {s}={{}}
for {i}=0,255 do {s}[{i}]={i} end
local {j}=0
for {i}=0,255 do
{j}=({j}+{s}[{i}]+{k}[{i}%#{k}+1])%256
{s}[{i}],{s}[{j}]={s}[{j}],{s}[{i}]
end
local {i},{j}=0,0
local {o}={{}}
for {n}=1,#{d} do
{i}=({i}+1)%256
{j}=({j}+{s}[{i}])%256
{s}[{i}],{s}[{j}]={s}[{j}],{s}[{i}]
{o}[{n}]={B}({S}.byte({d},{n}),{s}[({s}[{i}]+{s}[{j}])%256])
end
local {r}=""
for {i}=1,#{o},2000 do
local {c}={{}}
for {j}={i},{M}.min({i}+1999,#{o}) do {c}[#{c}+1]={o}[{j}] end
{r}={r}..{S}.char({T}.unpack({c}))
end
return {r}
end
"""

XOR_SRC = """local function {XOR}({d},{k})
local {o}={{}}
for {n}=1,#{d} do
{o}[{n}]={B}({S}.byte({d},{n}),{k}[({n}-1)%#{k}+1])
end
local {r}=""
for {i}=1,#{o},2000 do
local {c}={{}}
for {j}={i},{M}.min({i}+1999,#{o}) do {c}[#{c}+1]={o}[{j}] end
{r}={r}..{S}.char({T}.unpack({c}))
end
return {r}
end
"""

HELPER_SRC = """local function {H}({t})local {r}=""
for {i}=1,#{t},2000 do local {c}={{}}
for {j}={i},{M}.min({i}+1999,#{t}) do {c}[#{c}+1]={t}[{j}] end
{r}={r}..{S}.char({T}.unpack({c}))end;return {r} end
"""

PRELUDE = """local {S}=string;local {M}=math;local {T}=table;local {ID}="{hexid}";
local {LS}=loadstring or load;
do
local {D}=rawget(_G or {{}},"debug")
if type({D})=="table" then
local _ok,_h=pcall(rawget({D},"gethook") or function()end)
if _ok and _h~=nil then error("",0) end
end
end
local {B};do local _b=bit32;if _b then {B}=_b.bxor else {B}=function({x},{y})local {p},{q}=1,0;while {x}>0 or {y}>0 do if {x}%2~={y}%2 then {q}={q}+{p} end;{x},{y},{p}={M}.floor({x}/2),{M}.floor({y}/2),{p}*2 end;return {q} end end end
"""


def dec_escape(b):
    return "".join("\\%d" % x for x in b)


def build_layer(payload, rng, table_mode=None):
    names = Names(rng)
    table_mode = rng.random() < 0.5 if table_mode is None else table_mode
    base = {k: names.new() for k in "S M T ID LS D B RC4 XOR H".split()}
    base.update({k: names.new() for k in "x y p q d k s i j o n r c t".split()})
    keys = [bytes(rng.choice(ALNUM) for _ in range(64)) for _ in range(4)]
    kA, kB, kC, kD = keys
    enc = rc4(payload, kD)
    enc = xor_cycle(enc, kC)
    enc = xor_cycle(enc, kB)
    enc = rc4(enc, kA)
    key_names = [names.new() for _ in range(4)]
    parts = []
    parts.append(HEADER)
    parts.append(PRELUDE.format(hexid="".join(rng.choice("0123456789ABCDEF") for _ in range(20)), **base))
    parts.append(";--[[%s]]\n" % "".join(rng.choice("abcXYZ019") for _ in range(8)))
    blocks = []
    blocks.append(RC4_SRC.format(**base))
    blocks.append(XOR_SRC.format(**base))
    if table_mode:
        blocks.append(HELPER_SRC.format(**base))
    order = list(range(len(blocks)))
    rng.shuffle(order)
    first_chunk = names.new()
    parts.append("local %s=%d\n" % (first_chunk, rng.randint(1, 99)))
    parts.append("do local %s=%s.floor(%.5f*255)end\n" % (names.new(), base["M"], rng.random()))
    for idx in order:
        parts.append(blocks[idx])
    for kn, kb in zip(key_names, keys):
        parts.append("local %s={%s}\n" % (kn, ",".join(str(b) for b in kb)))
    blob_var = names.new()
    if table_mode:
        parts.append("local %s=%s({%s})\n" % (blob_var, base["H"], ",".join(str(b) for b in enc)))
        src = blob_var
    else:
        n_chunks = rng.randint(3, 8)
        size = max(1, -(-len(enc) // n_chunks))
        chunk_names = [first_chunk] + [names.new() for _ in range(n_chunks - 1)]
        pieces = [enc[i:i + size] for i in range(0, len(enc), size)]
        chunk_names = chunk_names[:len(pieces)]
        for cn, piece in zip(chunk_names, pieces):
            parts.append('local %s="%s"\n' % (cn, dec_escape(piece)))
        parts.append("local %s=%s\n" % (blob_var, "..".join(chunk_names)))
        src = blob_var
        parts.append("local %s=%s.len(\"%s\")\n" % (chunk_names[0], base["S"], "abc"))
    v1, v2, v3, v4, f, e = (names.new() for _ in range(6))
    parts.append("local %s=%s(%s,%s)\n" % (v1, base["RC4"], src, key_names[0]))
    parts.append("local %s=%s.max(0,%d)\n" % (names.new(), base["M"], rng.randint(1, 20)))
    parts.append("local %s=%s(%s,%s)\n" % (v2, base["XOR"], v1, key_names[1]))
    parts.append("local %s=%s(%s,%s)\n" % (v3, base["XOR"], v2, key_names[2]))
    parts.append('if false then error("",0)end\n')
    parts.append("local %s=%s(%s,%s)\n" % (v4, base["RC4"], v3, key_names[3]))
    parts.append("local %s,%s=%s(%s)\n" % (f, e, base["LS"], v4))
    parts.append("if not %s then error(tostring(%s),0) end\n" % (f, e))
    parts.append("%s()\n" % f)
    return "".join(parts).encode("latin-1")


def obfuscate(payload, seed=0, layers=3, table_mode=None):
    rng = random.Random(seed)
    cur = payload
    for _ in range(layers):
        cur = build_layer(cur, rng, table_mode)
    return cur


SAMPLE_PAYLOADS = [
    b'loadstring(game:HttpGet("https://example.com/a.lua"))()',
    b'print("hello")\nlocal t = {1,2,3}\nfor i,v in ipairs(t) do print(v) end\n-- trailing comment\n',
    bytes(range(256)) * 3,
    b"local s = [[line1\nline2]] --[==[ block ]==]\nreturn s\n",
]

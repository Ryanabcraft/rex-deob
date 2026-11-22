"""Cache of decoded constants, shared between runs of the same script.

Luraph decodes a constant lazily, so every run starts with the same thousands of
undecoded slots and pays a full rerun to decode them. The slots are identified by
a stable path ("12,U,57"), which is the same for the same input file -- so the
answer from one run is valid for the next one (a retry, a second round, or the
same script submitted again by another user).

Only table-free values are cached: a table would have to be copied into the new
dump with fresh ids, and a wrong remap would silently produce wrong code. Numbers,
strings, booleans and lists of those are copied verbatim, so nothing can drift.

The fingerprint of the dump's base tables guards against reusing a cache from a
different (or re-randomised) run: same script -> same fingerprint.
"""
import hashlib
import json
import os


def cache_dir():
    return os.environ.get("DEOB_CONST_CACHE") or os.path.join(
        os.path.expanduser("~"), ".deob_const_cache")


def key(source, salt=""):
    """Stable id for one script's source (the slot paths only make sense for it)."""
    data = source.encode("latin-1", "replace") if isinstance(source, str) else source
    if salt:
        data = data + b"\x00" + salt.encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()[:32]


def _path(k):
    return os.path.join(cache_dir(), k + ".json")


def _fingerprint(dump):
    """Cheap identity of the dump's base tables: (tid, array len, kv len)."""
    fp = []
    for tid in sorted((dump.get("tables") or {}), key=lambda t: int(t))[:512]:
        td = dump["tables"][tid]
        fp.append([int(tid), len(td.get("arr") or []), len(td.get("kv") or [])])
    return fp


def _sane(v):
    """A decoded value no real constant would look like: never cached, never trusted."""
    if isinstance(v, float):
        return v == v and abs(v) < 1e18
    if isinstance(v, str):
        return len(v) <= 1000000
    if isinstance(v, list):
        return all(_sane(x) for x in v)
    return True


def _simple(v):
    """True when the value carries no table reference (nothing to remap)."""
    if isinstance(v, dict):
        return False
    if isinstance(v, list):
        return all(not isinstance(x, dict) for x in v)
    return True


def _read(k):
    try:
        with open(_path(k), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load(k, dump, salt=""):
    """Merge the cached constants into `dump` (parsed protos). Returns how many
    were added, or 0 when there is no cache for this script / it does not match."""
    ent = _read(k)
    if not ent or ent.get("fp") != _fingerprint(dump) or ent.get("salt", "") != salt:
        return 0
    have = {p for p, _ in dump.get("overrides") or []}
    dump.setdefault("overrides", [])
    added = 0
    for p, v in ent.get("overrides") or []:
        if p in have or not _simple(v) or not _sane(v):
            continue
        dump["overrides"].append([p, v])
        added += 1
    return added


def store(k, dump, salt=""):
    """Remember this run's table-free decoded constants for the next run."""
    vals = [[p, v] for p, v in (dump.get("overrides") or []) if _simple(v) and _sane(v)]
    if not vals:
        return 0
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        tmp = _path(k) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fp": _fingerprint(dump), "salt": salt, "overrides": vals}, f)
        os.replace(tmp, _path(k))
        return len(vals)
    except Exception:
        return 0
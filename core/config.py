import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINES = os.path.join(ROOT, "engines")
TOOLS = os.path.join(ROOT, "tools")
WORK = os.environ.get("DEOB_WORK", os.path.join(ROOT, "work"))

PYTHON = sys.executable
IS_WIN = os.name == "nt"


def tool(name: str) -> str:
    return os.path.join(TOOLS, name + (".exe" if IS_WIN else ""))


def engine(*parts: str) -> str:
    return os.path.join(ENGINES, *parts)


PREFIX = os.environ.get("PREFIX", ".")
TOKEN = os.environ.get("DISCORD_TOKEN", "")

MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "10"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

COOLDOWN_S = float(os.environ.get("COOLDOWN_S", "8"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
JOB_TIMEOUT_S = int(os.environ.get("JOB_TIMEOUT_S", "1200"))
KEEP_JOBS = os.environ.get("KEEP_JOBS", "0") == "1"

TIMEOUTS = {
    "bytecode": int(os.environ.get("TIMEOUT_BYTECODE", "300")),
    "luraph15": int(os.environ.get("TIMEOUT_LURAPH15", "900")),
    "luraph14": int(os.environ.get("TIMEOUT_LURAPH14", "600")),
    "ib1": int(os.environ.get("TIMEOUT_IB1", "300")),
    "ironbrew2": int(os.environ.get("TIMEOUT_IRONBREW2", "240")),
    "luaobfuscator": int(os.environ.get("TIMEOUT_LUAOBFUSCATOR", "240")),
    "orrxl4": int(os.environ.get("TIMEOUT_ORRXL4", "300")),
    "ib2net": int(os.environ.get("TIMEOUT_IB2NET", "180")),
    "hercules": int(os.environ.get("TIMEOUT_HERCULES", "180")),
    "moonveil": int(os.environ.get("TIMEOUT_MOONVEIL", "300")),
    "ironveil": int(os.environ.get("TIMEOUT_IRONVEIL", "180")),
    "f77": int(os.environ.get("TIMEOUT_F77", "180")),
    "wyn": int(os.environ.get("TIMEOUT_WYN", "420")),
    "ib3": int(os.environ.get("TIMEOUT_IB3", "120")),
    "psu": int(os.environ.get("TIMEOUT_PSU", "300")),
    "synapsexen": int(os.environ.get("TIMEOUT_SYNAPSEXEN", "180")),
    "unveilr": int(os.environ.get("TIMEOUT_UNVEILR", "300")),
    "strings": int(os.environ.get("TIMEOUT_STRINGS", "120")),
    "prometheus": int(os.environ.get("TIMEOUT_PROMETHEUS", "240")),
    "moonsec": int(os.environ.get("TIMEOUT_MOONSEC", "300")),
    "centurion": int(os.environ.get("TIMEOUT_CENTURION", "600")),
    "luarmor": int(os.environ.get("TIMEOUT_LUARMOR", "300")),
}

ALLOWED_EXT = (".lua", ".luau", ".txt", ".lua5", ".zip")
LUA_EXT = (".lua", ".luau", ".txt", ".lua5")

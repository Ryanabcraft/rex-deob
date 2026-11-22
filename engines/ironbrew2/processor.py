import os
import sys
import importlib
from utils import asttools as parser
from utils.codec import instructionset, instructioncategories

payloadfile = sys.argv[1] if len(sys.argv) > 1 else "./input.lua"
outputfile = sys.argv[2] if len(sys.argv) > 2 else "restored.lua"

payload = ""
with open(payloadfile, "r") as f:
    payload = f.read()

syntaxtree = None
outputbuffer = None

try:
    sys.stdout.write("> parsing payload...")
    syntaxtree = parser.loadast(payload)
    print(" ok")
except Exception:
    print(" fail")
    sys.exit(1)

try:
    sys.stdout.write("> refining ast...")
    syntaxtree = parser.refineast(syntaxtree, {"renamevariables": True, "format": True})
    print(" ok")
except Exception as e:
    print(e)
    print(" fail")
    sys.exit(1)

for plugin in os.listdir("./modules"):
    try:
        mod = importlib.import_module(f"modules.{plugin}.detect")
        if mod.detect(payload):
            print(f"> engine identified: {mod.enginename}")
            outputbuffer = mod.execute(syntaxtree)
            break
    except:
        continue

if outputbuffer is not None:
    with open(outputfile, "w") as f:
        f.write(outputbuffer)
else:
    raise RuntimeError("no suitable engine found")

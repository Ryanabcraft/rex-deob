# Tool by @adi.codz (Discord)
"""Unit tests for luarmor_crack.probe (detection signatures, payload split)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luarmor_crack import probe as P  # noqa: E402

# A minimal synthetic client carrying 4+ signatures (section 9) plus a VM call,
# a payload closure and an IOC. Not a real client -- just enough to exercise the
# detector and splitter deterministically.
CLIENT = """-- bootstrap
if not ce_like_loadstring_fn then return end
local function fp() GetTutorialState("nil  nil  "..n) end
getfenv()[tbl17] = marker
writefile("luarmor-error-log.txt", msg)
local host = "us1-roblox-auth.luarmor.net"
local a, b, c, d, e, f, g, h, i = 1,2,3,4,5,6,7,8,9
luraph_runtime1(v83, buffer.fromstring("ABCD"), tbl33, 199)()
getgenv().SPECIAL_WEBHOOK_URL = "https://relay.workers.dev/d/alt/42"
Kick("[Luarmor]: invalid")
local x = v84[1] + v84[7]
"""

NOT_CLIENT = "print('just a normal script')\nlocal x = 1\n"

# The public v4 loader stub shape: a _bsdata0 fingerprint table, a -sephal
# module id, a cache path and the init URL. No client auth code at all.
STUB = """local f,b,a="static_content_170926","f07dbcbe19a-sephal";
pcall(function()a=readfile(f.."/init-"..b..".lua")end)
_bsdata0={1993097146,"\\164\\109",2453551814,79518837};
a=game:HttpGet("https://cdn.luarmor.net/v4_init_sephal.lua"..(_ca920af6193 or ""))
return loadstring(a)("f07dbcbe19a-sephal")
"""


class DetectTest(unittest.TestCase):
    def test_detects_client(self):
        hits, conf = P.detect(CLIENT)
        self.assertGreaterEqual(len(hits), 4)
        self.assertEqual(conf, 1.0)

    def test_rejects_plain(self):
        hits, conf = P.detect(NOT_CLIENT)
        self.assertEqual(hits, [])
        self.assertEqual(conf, 0.0)


class AnalyzeTest(unittest.TestCase):
    def _analyze(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return P.analyze(path)
        finally:
            os.unlink(path)

    def test_report_fields(self):
        r = self._analyze(CLIENT)
        self.assertTrue(r["luarmor_client"])
        self.assertTrue(r["embedded_vm_stage"]["present"])
        self.assertIn(1, r["opaque_constant_sites"]["indexes"])
        self.assertIn(7, r["opaque_constant_sites"]["indexes"])
        self.assertTrue(any(i["kind"] == "webhook_url" for i in r["iocs"]))
        self.assertIn("SPECIAL_WEBHOOK_URL", r["getgenv_globals"])

    def test_plain_not_client(self):
        r = self._analyze(NOT_CLIENT)
        self.assertFalse(r["luarmor_client"])
        self.assertNotIn("opaque_constant_sites", r)

    def test_split(self):
        with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
            f.write(CLIENT)
            path = f.name
        outdir = tempfile.mkdtemp()
        try:
            r = P.analyze(path)
            P.split_payload(path, outdir, r)
            self.assertTrue((Path(outdir) / "loader.lua").exists())
            self.assertTrue((Path(outdir) / "payload.lua").exists())
            self.assertTrue((Path(outdir) / "opaque.json").exists())
        finally:
            os.unlink(path)


class StubTest(unittest.TestCase):
    def test_kind_is_stub(self):
        self.assertEqual(P.detect_kind(STUB), "loader_stub")

    def test_client_is_not_stub(self):
        self.assertEqual(P.detect_kind(CLIENT), "client")

    def test_stub_report_roundtrips_bsdata0(self):
        r = P.render_stub_report(STUB)
        self.assertIn("f07dbcbe19a-sephal", r)
        self.assertIn("cdn.luarmor.net", r)
        self.assertIn("[4] = 79518837,", r)
        # a non-printable byte must come back as an escape, not raw
        self.assertIn("\\164", r)

    def test_stub_split_writes_report_lua(self):
        outdir = tempfile.mkdtemp()
        with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
            f.write(STUB)
            path = f.name
        try:
            r = P.analyze(path)
            P.split_payload(path, outdir, r)
            self.assertTrue((Path(outdir) / "report.lua").exists())
            self.assertFalse((Path(outdir) / "payload.lua").exists())
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()

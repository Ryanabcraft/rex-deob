import re
import unittest

from orrxl4 import sandbox
from orrxl4.reconstruct import reconstruct
from orrxl4.lua import parse

PROGRAM = b"""-- header comment
local _IlO0I1O0l1Il=string;local _O0Il10OI1l00=math
--[[ block
comment ]]
local _l1O0OI0lI1O1=45
do local _0O1lIl01OI00=_O0Il10OI1l00.floor(0.68469*255)end
if false then error("",0)end
local function _I0lO1l0O1Il0(_O10l0I1lOI0l,_1lIOO0l10IlI)
  local _lIO0l1OI0Il1={}
  for _0OlI1l0IO1lO=1,_1lIOO0l10IlI do _lIO0l1OI0Il1[#_lIO0l1OI0Il1+1]=_O10l0I1lOI0l*_0OlI1l0IO1lO end
  return _lIO0l1OI0Il1
end
local _l1O0OI0lI1O1={tag="caf\\195\\169 --not a comment",f=function(a) return a+1 end,[2]=7}
local r=_I0lO1l0O1Il0(3,4)
print(r[1],r[4],#r,_l1O0OI0lI1O1.tag,_l1O0OI0lI1O1.f(1),_l1O0OI0lI1O1[2],_IlO0I1O0l1Il.rep("a",3))
print(1)
;(print)("tail")
"""


class Reconstruct(unittest.TestCase):
    def setUp(self):
        self.out = reconstruct(PROGRAM)
        self.text = self.out.decode("latin-1")

    def test_comments_and_junk_removed(self):
        self.assertNotIn("header comment", self.text)
        self.assertNotIn("block", self.text)
        self.assertNotIn("if false", self.text)
        self.assertNotIn("floor(0.68469", self.text)
        self.assertNotIn("= 45", self.text)
        self.assertEqual(self.text.count("--"), 1)
        self.assertIn("--not a comment", self.text)

    def test_names_are_readable(self):
        self.assertIsNone(re.search(r"_[IlO01]{6,}", self.text))
        self.assertRegex(self.text, r"func_1")
        self.assertRegex(self.text, r"arg_1")

    def test_semantics_preserved(self):
        a = sandbox.run(PROGRAM, timeout=10)
        b = sandbox.run(self.out, timeout=10)
        self.assertIsNone(a.error)
        self.assertIsNone(b.error)
        self.assertEqual(a.prints, b.prints)
        self.assertEqual(a.prints[0].split(b"\t")[3], "café --not a comment".encode("utf-8"))

    def test_output_parses_and_is_stable(self):
        parse(self.text)
        self.assertEqual(reconstruct(self.out), self.out)

    def test_keep_names(self):
        kept = reconstruct(PROGRAM, rename=False).decode("latin-1")
        self.assertIn("_I0lO1l0O1Il0", kept)

    def test_unparseable_input_falls_back_to_comment_stripping(self):
        out = reconstruct(b"-- c\n::label:: goto label\nprint('--x') -- tail\n")
        self.assertNotIn(b"tail", out)
        self.assertIn(b"print('--x')", out)

    def test_unlexable_input_does_not_raise(self):
        out = reconstruct(b"-- c\n!!bad ?? stuff\nprint(1)\n")
        self.assertIn(b"print(1)", out)

    def test_c_style_not_equal_is_accepted(self):
        out = reconstruct(b"local a=1\nif a != 2 then print(a) end").decode("latin-1")
        self.assertIn("~=", out)


if __name__ == "__main__":
    unittest.main()

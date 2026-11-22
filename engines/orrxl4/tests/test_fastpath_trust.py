import re
import unittest

from orrxl4.layers import peel
from tests.gen_sample import build_layer
import random


class FastPathTrust(unittest.TestCase):
    def test_redefined_bxor_is_not_shortcut(self):
        rng = random.Random(7)
        data = build_layer(b'print("hello")', rng, table_mode=False).decode("latin-1")
        name = re.search(r"local (\w+);do local _b=bit32", data).group(1)
        data = re.sub(r"local %s;do local _b=bit32.*?end end end\n" % name, "local %s=function(a,b) return (a+b)%%256 end\n" % name, data, flags=re.S)
        self.assertIn("(a+b)%256", data)
        raw = data.encode("latin-1")
        try:
            fast = peel(raw, fast=True)[0]
        except Exception as e:
            fast = repr(e)
        try:
            slow = peel(raw, fast=False)[0]
        except Exception as e:
            slow = repr(e)
        self.assertEqual(fast, slow)
        self.assertNotEqual(fast, b'print("hello")')


if __name__ == "__main__":
    unittest.main()

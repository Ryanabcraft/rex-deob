import os
import unittest

from orrxl4 import deobfuscate_bytes
from orrxl4.layers import peel, has_header
from tests.gen_sample import obfuscate, SAMPLE_PAYLOADS

HERE = os.path.dirname(__file__)
EXPECTED = b'loadstring(game:HttpGet("https://raw.githubusercontent.com/ynqo/key/refs/heads/main/obfuscated_script-1794035911766.lua.txt"))()'


class RealSample(unittest.TestCase):
    def test_uploaded_sample(self):
        with open(os.path.join(HERE, "sample_orrxl4.lua"), "rb") as fh:
            data = fh.read()
        self.assertTrue(has_header(data))
        final, layers = peel(data)
        self.assertEqual(final, EXPECTED)
        self.assertEqual(len(layers), 3)
        result = deobfuscate_bytes(data)
        self.assertEqual(result.clean.strip(), EXPECTED)
        self.assertNotIn(b"--", result.clean.replace(b"https://", b""))


class Generated(unittest.TestCase):
    def test_random_layouts(self):
        for seed in range(24):
            payload = SAMPLE_PAYLOADS[seed % len(SAMPLE_PAYLOADS)]
            layers = 1 + seed % 5
            mode = [None, True, False][seed % 3]
            data = obfuscate(payload, seed=seed, layers=layers, table_mode=mode)
            final, found = peel(data)
            self.assertEqual(final, payload, "seed %d" % seed)
            self.assertEqual(len(found), layers, "seed %d" % seed)

    def test_large_payload(self):
        payload = (b"local x = 1\n" * 20000)
        data = obfuscate(payload, seed=99, layers=2, table_mode=False)
        final, found = peel(data)
        self.assertEqual(final, payload)


if __name__ == "__main__":
    unittest.main()

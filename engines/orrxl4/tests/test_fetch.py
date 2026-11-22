import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from orrxl4 import deobfuscate, Fetcher, FetchError
from orrxl4.fetch import to_url, is_html, accept
from tests.gen_sample import obfuscate

HTML = b"<!DOCTYPE html><html><head><title>ORRXL4 PROTECTOR</title></head><body>protected</body></html>"


def make_server(routes, html_first=0, need_ua=None):
    state = {"count": 0, "agents": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["count"] += 1
            ua = self.headers.get("User-Agent", "")
            state["agents"].append(ua)
            body = routes.get(self.path)
            blocked = state["count"] <= html_first or (need_ua and need_ua not in ua)
            if body is None or blocked:
                data, ctype, code = HTML, "text/html; charset=UTF-8", 200
            else:
                data, ctype, code = body, "text/plain", 200
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


class FetchLogic(unittest.TestCase):
    def test_html_detection_and_target_parsing(self):
        self.assertTrue(is_html(HTML))
        self.assertFalse(is_html(b"-- PROTECTED BY ORRXL4 OBFUSCATOR\n<html>"))
        self.assertTrue(accept(b"\xef\xbb\xbf\n-- PROTECTED BY ORRXL4 OBFUSCATOR\nx", True))
        self.assertFalse(accept(HTML, True))
        self.assertEqual(to_url("58mm7qon"), "https://orrxl4-protector.com/api/raw?id=58mm7qon")
        self.assertEqual(to_url("https://a.b/c"), "https://a.b/c")
        self.assertIsNone(to_url("not a target!"))

    def test_retries_until_header_and_rotates_profiles(self):
        loader = obfuscate(b'print("ok")', seed=3, layers=2)
        server, state = make_server({"/api/raw?id=abc12345": loader}, html_first=3, need_ua="Roblox")
        try:
            base = "http://127.0.0.1:%d" % server.server_port
            fetcher = Fetcher(retries=12, delay=0, jitter=0)
            body = fetcher.fetch(base + "/api/raw?id=abc12345")
            self.assertTrue(body.startswith(b"-- PROTECTED BY ORRXL4 OBFUSCATOR"))
            self.assertGreaterEqual(state["count"], 4)
        finally:
            server.shutdown()
            server.server_close()

    def test_gives_up_when_only_html(self):
        server, state = make_server({})
        try:
            fetcher = Fetcher(retries=4, delay=0, jitter=0)
            with self.assertRaises(FetchError):
                fetcher.fetch("http://127.0.0.1:%d/x" % server.server_port)
            self.assertEqual(state["count"], 4)
            self.assertGreater(len(set(state["agents"])), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_custom_user_agent_is_tried_first(self):
        server, state = make_server({"/p": obfuscate(b"x=1", seed=1, layers=1)})
        try:
            fetcher = Fetcher(retries=2, delay=0, jitter=0, user_agent="MyAgent/9")
            fetcher.fetch("http://127.0.0.1:%d/p" % server.server_port)
            self.assertEqual(state["agents"][0], "MyAgent/9")
        finally:
            server.shutdown()
            server.server_close()

    def test_full_chain_with_follow(self):
        routes = {}
        server, state = make_server(routes, html_first=2, need_ua="Roblox")
        base = "http://127.0.0.1:%d" % server.server_port
        stage1 = ('loadstring(game:HttpGet("%s/stage2"))()' % base).encode()
        routes["/stage1"] = obfuscate(stage1, seed=11, layers=3)
        routes["/stage2"] = obfuscate(b'print("final script")', seed=12, layers=2)
        try:
            fetcher = Fetcher(retries=10, delay=0, jitter=0)
            results = deobfuscate(base + "/stage1", fetcher=fetcher, follow=True)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0].final, stage1)
            self.assertEqual(results[1].final, b'print("final script")')
            self.assertEqual(len(results[0].layers), 3)
            one = deobfuscate(base + "/stage1", fetcher=fetcher, follow=False)
            self.assertEqual(len(one), 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

"""Tests for the phone server. Standard library only.

Run:  python -m unittest discover -s tests -v

Covers message handling, language strings, page rendering, the HTTP API
(auth, messages, upload) and path safety. The desktop automation (UI
Automation, SendKeys) is NOT covered: it needs a live Hermes Desktop window.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402

TOKEN = "test-token-123"


def make_db(path, rows, session_id="s1", title="Bot Chat"):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT, title TEXT, started_at REAL)")
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                "content TEXT, timestamp REAL)")
    con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, "desktop", title, 1.0))
    for i, (role, content) in enumerate(rows, start=1):
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?)", (i, session_id, role, content, float(i)))
    con.commit()
    con.close()


class TestDisplayText(unittest.TestCase):
    def test_plain_text_passes_through(self):
        self.assertEqual(srv._display_text("hello"), "hello")

    def test_long_text_is_cut(self):
        out = srv._display_text("x" * (srv.SHOW_CHARS + 500))
        self.assertTrue(out.endswith(" …"))
        self.assertLessEqual(len(out), srv.SHOW_CHARS + 2)

    def test_multimodal_keeps_text_and_marks_image(self):
        raw = ('\x00json:[{"type": "text", "text": "Look at this"}, '
               '{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]')
        self.assertEqual(srv._display_text(raw), "Look at this\n[Bild]")

    def test_multimodal_without_text_shows_only_marker(self):
        raw = '\x00json:[{"type": "image_url", "image_url": {"url": "data:x"}}]'
        self.assertEqual(srv._display_text(raw), "[Bild]")

    def test_escaped_characters_are_decoded(self):
        raw = '\x00json:[{"type": "text", "text": "say \\"hi\\"\\nnext"}]'
        self.assertEqual(srv._display_text(raw), 'say "hi"\nnext')


class TestLoadMessages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "state.db")
        self.saved = dict(srv.BOTS)

    def tearDown(self):
        srv.BOTS.clear()
        srv.BOTS.update(self.saved)

    def use(self, rows, title="Bot Chat"):
        make_db(self.db, rows, title=title)
        srv.BOTS.clear()
        srv.BOTS["bot"] = {"label": "Bot", "ui_name": "@bot", "db": self.db}

    def test_skips_empty_and_tool_rows(self):
        self.use([("user", "hi"), ("assistant", ""), ("tool", "output"), ("assistant", "ok")])
        self.assertEqual([m["text"] for m in srv.load_messages("bot")], ["hi", "ok"])

    def test_keeps_only_newest_messages_in_order(self):
        rows = [("user", f"m{i}") for i in range(srv.MSG_LIMIT + 30)]
        self.use(rows)
        msgs = srv.load_messages("bot")
        self.assertEqual(len(msgs), srv.MSG_LIMIT)
        self.assertEqual(msgs[-1]["text"], f"m{srv.MSG_LIMIT + 29}")
        self.assertEqual(msgs[0]["text"], "m30")

    def test_huge_message_does_not_blow_up_payload(self):
        # A 5 MB message must not end up in the payload (the real failure was a 64 MB row).
        self.use([("user", "short " + "A" * 5_000_000)])
        msgs = srv.load_messages("bot")
        self.assertEqual(len(msgs), 1)
        self.assertLess(len(json.dumps(msgs)), 10_000)

    def test_no_session_returns_empty(self):
        self.use([("user", "hi")], title="Other")
        srv.BOTS["bot"]["db"] = os.path.join(self.tmp, "missing.db")
        self.assertEqual(srv.load_messages("bot"), [])


class TestLanguage(unittest.TestCase):
    def setUp(self):
        self.saved = srv.LANG

    def tearDown(self):
        srv.LANG = self.saved

    def test_both_languages_have_the_same_keys(self):
        self.assertEqual(set(srv.STR["en"]), set(srv.STR["de"]))

    def test_t_formats_arguments(self):
        srv.LANG = "en"
        self.assertIn("'Hermes'", srv.T("err_window_missing", w="Hermes"))

    def test_page_has_no_untranslated_placeholders(self):
        for lang in ("en", "de"):
            srv.LANG = lang
            self.assertNotIn("{{", srv.render_page(), lang)

    def test_page_switches_language(self):
        srv.LANG = "en"
        self.assertIn(srv.STR["en"]["send"], srv.render_page())
        srv.LANG = "de"
        self.assertIn(srv.STR["de"]["send"], srv.render_page())


class TestHttpApi(unittest.TestCase):
    """Starts the real HTTP server on localhost with a temp database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "state.db")
        make_db(cls.db, [("user", "first"), ("assistant", "second")])
        cls.saved_bots, cls.saved_host, cls.saved_upload = dict(srv.BOTS), srv.HOST, srv.UPLOAD_DIR
        srv.BOTS.clear()
        srv.BOTS["bot"] = {"label": "Bot", "ui_name": "@bot", "db": cls.db}
        srv.UPLOAD_DIR = os.path.join(cls.tmp, "uploads")
        srv.HOST = "127.0.0.1"
        cls.saved_refresh = srv.refresh_bots
        srv.refresh_bots = lambda: None  # keep the test bots; do not rescan real profiles
        cls.httpd = srv.start_server("127.0.0.1", 0, TOKEN)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        srv.refresh_bots = cls.saved_refresh
        srv.BOTS.clear()
        srv.BOTS.update(cls.saved_bots)
        srv.HOST, srv.UPLOAD_DIR = cls.saved_host, cls.saved_upload

    def get(self, path, token=TOKEN):
        req = urllib.request.Request(self.base + path + ("&" if "?" in path else "?") + f"t={token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def post(self, path, body, ctype="application/json", token=TOKEN):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path + f"?t={token}", data=data,
                                     headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_rejects_missing_token(self):
        self.assertEqual(self.get("/api/version", token="wrong")[0], 403)

    def test_version(self):
        status, body = self.get("/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], srv.VERSION)

    def test_messages(self):
        status, body = self.get("/api/messages?bot=bot")
        self.assertEqual(status, 200)
        texts = [m["text"] for m in json.loads(body)["messages"]]
        self.assertEqual(texts, ["first", "second"])

    def test_upload_rejects_non_image(self):
        self.assertEqual(self.post("/api/upload", b"hello", ctype="text/plain")[0], 415)

    def test_upload_accepts_png(self):
        status, body = self.post("/api/upload", b"\x89PNG fake", ctype="image/png")
        self.assertEqual(status, 200)
        name = json.loads(body)["file"]
        self.assertTrue(os.path.isfile(os.path.join(srv.UPLOAD_DIR, name)))

    def test_send_image_refuses_path_traversal(self):
        status, _ = self.post("/api/send_image", {"bot": "bot", "file": "../../etc/passwd"})
        self.assertEqual(status, 400)

    def test_send_refuses_inactive_bot(self):
        # Writes are only allowed for the bot that is active in the desktop.
        srv.BOTS["other"] = {"label": "Other", "ui_name": "@other", "db": self.db}
        try:
            status, _ = self.post("/api/send", {"bot": "other", "text": "hi"})
            self.assertEqual(status, 403)
        finally:
            srv.BOTS.pop("other", None)


if __name__ == "__main__":
    unittest.main()

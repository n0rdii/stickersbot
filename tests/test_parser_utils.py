"""tests for parser/parser.py helpers (no network, no model)."""
import base64
import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parser"))

import parser as parser_mod  # noqa: E402  (parser/parser.py)


class TestParsePackNames(unittest.TestCase):
    def test_links_names_comments_dedup(self):
        text = (
            "https://t.me/addstickers/tg_1\n"
            "t.me/addstickers/tg_2\n"
            "# commented out\n"
            "tg_3\n"
            "@tg_4\n"
            "tg_1\n"
            "not a pack line\n"
            "https://t.me/addstickers/TG_4\n"      # case-insensitive dup
        )
        self.assertEqual(parser_mod.parse_pack_names(text),
                         ["tg_1", "tg_2", "tg_3", "tg_4", "TG_4"])


class TestParseJsonRecord(unittest.TestCase):
    def test_normalizes_a_good_answer(self):
        raw = "```json\n" + json.dumps({
            "description": "  a   sleepy  wolf  ",
            "characters": ["wolf"],
            "objects": [],
            "actions": "sleeping",
            "emotions": [], "visual_tags": [], "style_tags": [],
            "nsfw": False,
        }) + "```"
        rec = parser_mod.parse_json_record(raw)
        self.assertEqual(rec["description"], "a sleepy wolf")
        self.assertEqual(rec["characters"], ["wolf"])
        self.assertEqual(rec["actions"], ["sleeping"])      # a single string becomes a list
        self.assertIs(rec["nsfw"], False)

    def test_garbage_raises(self):
        for text in ("", "hello world", "[]", '{"description": ""}'):
            with self.assertRaises(ValueError):
                parser_mod.parse_json_record(text)

    def test_bad_nsfw_becomes_none(self):
        rec = parser_mod.parse_json_record('{"description": "a dog", "nsfw": "yes"}')
        self.assertIsNone(rec["nsfw"])


class TestCleanList(unittest.TestCase):
    def test_dedupes_normalizes_and_caps(self):
        out = parser_mod.clean_list(["A  b", "a b", 5, "a  b", ["x"]])
        self.assertEqual(out, ["A b"])
        self.assertEqual(parser_mod.clean_list(["x"] * 40), ["x"])   # dedupe is case-insensitive
        self.assertEqual(len(parser_mod.clean_list([f"p{i:02}" for i in range(40)])), 30)


class TestApprovedRequests(unittest.TestCase):
    def test_reads_only_approved_and_fuzzy_names_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "b.db"
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE pack_requests(pack TEXT, status TEXT, created_ts INTEGER)")
            db.executemany("INSERT INTO pack_requests VALUES (?,?,?)", [
                ("good_one", "approved", 1),
                ("bad one", "approved", 2),
                ("pending_pack", "pending", 3),
            ])
            db.commit()
            db.close()
            self.assertEqual(parser_mod.approved_requests(str(db_path)), ["good_one"])
            self.assertEqual(parser_mod.approved_requests(str(Path(tmp) / "nope.db")), [])
            self.assertEqual(parser_mod.approved_requests(""), [])


class TestPrepareImage(unittest.TestCase):
    def test_flattens_transparency_and_downscales(self):
        import PIL.Image as PILImage

        im = PILImage.new("RGBA", (800, 800), (255, 0, 0, 0))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        b64 = parser_mod.prepare_image(buf.getvalue())
        out = PILImage.open(io.BytesIO(base64.b64decode(b64)))
        self.assertEqual(out.mode, "RGB")
        self.assertLessEqual(out.width, 512)
        px = out.getpixel((5, 5))
        self.assertGreaterEqual(sum(px[:3]), 600)            # no black corners


class _Response:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, body):
        self.body = body
        self.url = self.headers = None

    def post(self, url, json=None, headers=None):
        self.url, self.headers = url, headers
        return _Response(self.body)


class TestPostprocessEndpoint(unittest.TestCase):
    """The postprocessor talks to any OpenAI-compatible endpoint; a local one
    (Ollama) needs no OpenRouter key."""

    def go(self, coro):
        return asyncio.run(coro)

    def test_local_endpoint_needs_no_key(self):
        cfg = types.SimpleNamespace(or_model="qwen2.5:14b", pp_max_tokens=100, pp_reasoning="",
                                    pp_url="http://127.0.0.1:11434/v1/chat/completions")
        valid = '{"description": "a cat", "nsfw": false}'
        s = _Session({"choices": [{"message": {"content": valid}}]})
        self.assertEqual(self.go(parser_mod.postprocess(s, cfg, "cap", "x")),
                         parser_mod.parse_json_record(valid))
        self.assertEqual(s.url, cfg.pp_url)
        self.assertIsNone(s.headers)                          # no Authorization header

    def test_default_points_at_openrouter(self):
        self.assertIn("openrouter.ai", parser_mod.parse_args([]).pp_url)


if __name__ == "__main__":
    unittest.main()

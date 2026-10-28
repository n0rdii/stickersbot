"""smoke test: the full indexing graph runs end-to-end on fakes (no network, no model)."""
import asyncio
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parser"))

import parser as parser_mod  # noqa: E402  (parser/parser.py)
import graph as graph_mod    # noqa: E402  (parser/graph.py, needs langgraph)


def _png_bytes():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", (48, 48), (200, 0, 0, 128)).save(b, "PNG")
    return b.getvalue()


def _dumps(rec):
    return json.dumps(rec, ensure_ascii=False)


class _FakeTG:
    def __init__(self):
        self.downloaded = 0

    async def get_pack(self, name):
        stickers = [types.SimpleNamespace(file_unique_id=f"{name}_u{i}", file_id=f"{name}_f{i}",
                                          is_animated=False, is_video=False,
                                          thumbnail=None, emoji="🐾")
                    for i in (1, 2)]
        return types.SimpleNamespace(name=name, title=f"Test {name}", stickers=stickers)

    async def download(self, _file_id):
        self.downloaded += 1
        return _png_bytes()

    async def close(self):
        pass


class _FakeEmbedder:
    model_id, dim = "fake-hash-bow", 384

    def passages(self, texts, batch=16):
        return [np.ones(self.dim, dtype=np.float32) / self.dim ** 0.5 for _ in texts]


class _Resp:
    status = 200
    headers = {}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """serves JoyCaption at :8080 and the postprocessor (Ollama-style) at :11434."""

    def post(self, url, json=None, headers=None):
        if ":11434" in url:
            cap = json["messages"][-1]["content"]
            rec = {"description": cap, "characters": ["wolf"], "objects": [], "actions": [],
                   "emotions": [], "visual_tags": ["wolf"], "style_tags": [],
                   "nsfw": "spicy" in cap}
            return _Resp({"choices": [{"message": {"content": _dumps(rec)}}]})
        return _Resp({"choices": [{"message": {"content": "a gray wolf waving its paw"}}]})


class TestIndexGraph(unittest.TestCase):
    def test_end_to_end_dedupe_and_reject(self):
        with tempfile.TemporaryDirectory() as tmp:

            def cfg():
                return types.SimpleNamespace(
                    limit=0, dl_concurrency=2, joy_concurrency=1, pp_concurrency=2,
                    emb_batch=2, joy_url="http://127.0.0.1:8080/v1/chat/completions",
                    joy_model="joycaption", joy_system="s", joy_max_tokens=64,
                    pp_url="http://127.0.0.1:11434/v1/chat/completions", or_model="qwen2.5:14b",
                    pp_max_tokens=100, pp_reasoning="", model_dir=str(Path(tmp) / "mn"))

            tg = _FakeTG()
            db = parser_mod.open_db(str(Path(tmp) / "s.db"))
            cache = sqlite3.connect(str(Path(tmp) / "raw.db"))
            cache.execute("CREATE TABLE IF NOT EXISTS raw"
                          "(uid TEXT PRIMARY KEY, caption TEXT, ts INTEGER)")
            # one caption cached beforehand, and it is spicy: that sticker must be rejected
            raw_cache = {"pack_a_u2": "a spicy pose of a wolf"}
            counters = {k: 0 for k in ("saved", "rejected", "failed", "dup", "no_thumb",
                                       "packs_bad", "captioned", "dropped")}

            async def go(packs, known, session):
                graph, flush = graph_mod.build_pipeline(
                    cfg(), tg, session, _FakeEmbedder(), db, cache, raw_cache, packs, known,
                    counters, asyncio.Event(),
                    "Describe the sticker.", "Turn the caption into JSON: {caption}")
                await graph.ainvoke({}, config={"recursion_limit": 64})
                await flush()

            session = _Session()
            packs = ["pack_a", "pack_b"]
            known = set()
            asyncio.run(go(packs, known, session))

            self.assertEqual(counters["saved"], 4)          # all four land in stickers
            self.assertEqual(counters["rejected"], 0)
            self.assertEqual(counters["failed"], 0)
            self.assertEqual(counters["captioned"], 3)      # the cached one is not re-captioned
            self.assertEqual(tg.downloaded, 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT nsfw FROM stickers WHERE file_unique_id=?",
                                        ("pack_a_u2",)).fetchone()[0], 1)   # spicy -> flagged
            self.assertEqual(db.execute("SELECT nsfw FROM stickers WHERE file_unique_id=?",
                                        ("pack_a_u1",)).fetchone()[0], 0)
            self.assertEqual(len(db.execute(
                "SELECT rowid FROM fts_stickers WHERE fts_stickers MATCH 'wolf'").fetchall()), 4)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()[0],
                             "384")
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='model'").fetchone()[0],
                             "mn")
            self.assertEqual(cache.execute("SELECT COUNT(*) FROM raw").fetchone()[0], 3)

            # resumable run: the same packs again -> everything counts as known, nothing saved
            asyncio.run(go(packs, known, session))
            self.assertEqual(counters["dup"], 4)
            self.assertEqual(counters["saved"], 4)
            db.close()
            cache.close()


if __name__ == "__main__":
    unittest.main()

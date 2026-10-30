"""tools/eval_search.py: metric math and label parsing (no DB, no model)."""
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import eval_search as ev  # noqa: E402
from test_search import make_db  # noqa: E402  (synthetic DB + fake embedder helper)


def res(*packs):
    return [{"set_name": p} for p in packs]


class TestQueryMetrics(unittest.TestCase):
    def test_hit_rr_recall_ndcg(self):
        m = ev.query_metrics(res("a", "b", "c", "d"), {"b", "d"}, k=4)
        self.assertEqual(m["hit"], 1.0)
        self.assertAlmostEqual(m["rr"], 0.5)
        self.assertAlmostEqual(m["recall"], 1.0)
        dcg = 1 / math.log2(3) + 1 / math.log2(5)
        idcg = 1 + 1 / math.log2(3)
        self.assertAlmostEqual(m["ndcg"], dcg / idcg)

    def test_miss_and_pack_dedupe(self):
        m = ev.query_metrics(res("x", "x", "y"), {"z"}, k=10)
        self.assertEqual((m["hit"], m["rr"], m["recall"]), (0.0, 0.0, 0.0))
        m = ev.query_metrics(res("x", "x", "y"), {"y"}, k=10)
        self.assertAlmostEqual(m["rr"], 0.5)          # repeated pack counts once

    def test_cutoff(self):
        self.assertEqual(ev.query_metrics(res("a", "b", "c"), {"c"}, k=2)["hit"], 0.0)
        self.assertEqual(ev.query_metrics(res("a", "b", "c"), {"c"}, k=3)["hit"], 1.0)


class TestLoadLabels(unittest.TestCase):
    def test_comments_blanks_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "q.jsonl"
            p.write_text('# comment\n\n{"query": "cat", "packs": ["a"]}\n', encoding="utf-8")
            self.assertEqual(ev.load_labels(p), [{"query": "cat", "packs": ["a"]}])
            for bad in ('{"packs": ["a"]}', '{"query": "cat"}', '{"query": " ", "packs": ["a"]}',
                        "not json"):
                p.write_text(bad + "\n", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    ev.load_labels(p)


class TestMain(unittest.TestCase):
    def test_runs_against_a_synthetic_db(self):
        import contextlib
        import io as sio
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "s.db")
            make_db(db, [("cats", "a sleeping cat on a pillow", ["sleeping"], 0),
                         ("dogs", "a happy dog dancing", [], 0)])
            labels = Path(tmp) / "q.jsonl"
            labels.write_text('{"query": "sleeping cat", "packs": ["cats"]}\n', encoding="utf-8")
            buf = sio.StringIO()
            argv = sys.argv
            try:
                sys.argv = ["eval_search.py", "--db", db, "--labels", str(labels), "-k", "5"]
                with contextlib.redirect_stdout(buf):
                    ev.main()
            finally:
                sys.argv = argv
            out = buf.getvalue()
            self.assertRegex(out, r"sleeping cat\s+1\s+1\.000")
            self.assertIn("MEAN", out)


if __name__ == "__main__":
    unittest.main()

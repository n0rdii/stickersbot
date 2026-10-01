"""Search tests: FTS/ranking/logging over a synthetic DB with a fake embedder."""
import asyncio
import hashlib
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parser"))
sys.path.insert(0, str(ROOT / "bot"))          # bot/ first: textclean + (fake) embedder

DIM = 384


class FakeEmbedder:
    model_id, dim = "fake-hash-bow", DIM

    def __init__(self, *a, **k):
        pass

    def _vec(self, text):
        v = np.zeros(DIM, dtype=np.float32)
        for w in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def query(self, text):
        return self._vec(text)

    def passages(self, texts, batch=16):
        return [self._vec(t) for t in texts]


sys.modules["embedder"] = types.SimpleNamespace(Embedder=FakeEmbedder)

import bot as botmod                           # noqa: E402
import spellfix                                # noqa: E402
import reindex                                 # noqa: E402
import textclean                               # noqa: E402
from store import Store                        # noqa: E402

LIST_KEYS = textclean.FTS_LIST_KEYS
SCHEMA = """CREATE TABLE stickers(id INTEGER PRIMARY KEY, file_unique_id TEXT UNIQUE NOT NULL,
  file_id TEXT NOT NULL, set_name TEXT, set_title TEXT, emoji TEXT, kind TEXT, description TEXT,
  characters TEXT, objects TEXT, actions TEXT, emotions TEXT, visual_tags TEXT, style_tags TEXT,
  nsfw INTEGER DEFAULT 0, emb BLOB NOT NULL, added_ts INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"""


def make_db(path, rows, fts_sql=None):
    import json
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute(fts_sql or textclean.fts_create_sql())
    emb = FakeEmbedder()
    for i, (pack, desc, actions, nsfw) in enumerate(rows, 1):
        lists = {k: [] for k in LIST_KEYS}
        lists["actions"] = actions
        db.execute("INSERT INTO stickers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (i, f"u{i}", f"f{i}", pack, pack, "", "static", desc,
                    *[json.dumps(lists[k]) for k in LIST_KEYS], nsfw,
                    emb.query(textclean.index_description(desc)).tobytes(), 0))
        db.execute("INSERT INTO fts_stickers(rowid,description,characters,objects,actions,emotions,"
                   "visual_tags,style_tags,emoji,set_title) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (i, *textclean.fts_values(desc, lists, "", pack)))
    db.execute("INSERT INTO meta VALUES('dim','384')")
    db.commit()
    db.close()


def cfg(db, **kw):
    d = dict(db=db, top_k=50, fts_top=120, vec_top=120, fts_weight=1.0, vec_weight=1.0,
             hide_nsfw=False, pref_boost=2.0, nsfw_penalty=0.5, nsfw_share_threshold=0.5,
             model_dir="", emb_device="", max_per_pack=3, diverse_top=10, pop_weight=0.05,
             log_settle=0.05)
    d.update(kw)
    return types.SimpleNamespace(**d)


class TextClean(unittest.TestCase):
    def test_strips_filler_keeps_meaning(self):
        f = textclean.strip_idle
        self.assertEqual(f("A gray wolf stands in an idle pose."), "A gray wolf stands.")
        self.assertEqual(f("A fox standing idle with one arm raised."),
                         "A fox standing with one arm raised.")
        self.assertEqual(f("An idle game icon."), "An idle game icon.")   # not filler
        self.assertEqual(f("A dragon with red eyes and an idle pose."), "A dragon with red eyes.")

    def test_index_list_drops_filler_items(self):
        self.assertEqual(textclean.index_list(["idle pose", "idle", "waving", "sitting idle",
                                               "idle pose with arms crossed"]),
                         ["waving", "sitting", "arms crossed"])

    def test_bot_and_parser_copies_do_not_drift(self):
        self.assertEqual((ROOT / "bot" / "textclean.py").read_bytes(),
                         (ROOT / "parser" / "textclean.py").read_bytes())


class FtsQuery(unittest.TestCase):
    q = staticmethod(botmod.build_fts_queries)

    def test_stopwords_dropped(self):
        self.assertEqual(self.q("wolf with a gun"),
                         ('"wolf" AND "gun"*', '"wolf" OR "gun"*'))

    def test_trailing_stopword_means_last_real_word_is_final(self):
        self.assertEqual(self.q("wolf with"), ('"wolf"', '"wolf"'))

    def test_unfinished_short_tail_dropped(self):
        self.assertEqual(self.q("wolf wi"), ('"wolf"', '"wolf"'))
        self.assertEqual(self.q("kn"), ('"kn"*', '"kn"*'))          # the only word is kept

    def test_only_stopwords_kept(self):
        self.assertEqual(self.q("the"), ('"the"*', '"the"*'))

    def test_no_fts_syntax_injection(self):
        and_q, or_q = self.q('wolf" OR (x NEAR/3 y) -z * "')
        self.assertNotIn("NEAR", and_q.replace('"near"', ""))
        self.assertTrue(re.fullmatch(r'("\w+"\*? (AND|OR) )*"\w+"\*?', and_q))
        self.assertIsNone(self.q("!!! ??"))


class SearchBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "stickers.db")
        self.store = Store(str(Path(self.tmp.name) / "bot_data.db"))

    def engine(self, rows, **kw):
        make_db(self.db, rows)
        e = botmod.HybridSearch(cfg(self.db, **kw), self.store)
        self.addCleanup(e.close)
        return e

    def test_all_words_beat_rare_word_alone(self):
        e = self.engine([("a", "A brown dog holding a mug, looking sleepy.", [], 0),
                         ("b", "A gray wolf yawning, sleepy and tired.", [], 0),
                         ("c", "A gray wolf waving.", [], 0)], vec_weight=0.0)
        self.assertEqual(e.search("sleepy wolf")[0]["sid"], 2)      # has BOTH words

    def test_stemming(self):
        e = self.engine([("a", "A cat sleeps on a pillow.", [], 0),
                         ("b", "A dog barking.", [], 0)], vec_weight=0.0)
        self.assertEqual([r["sid"] for r in e.search("sleeping cat")][:1], [1])

    def test_returns_up_to_50(self):
        rows = [(f"p{i}", f"A wolf number {i} howling.", [], 0) for i in range(80)]
        e = self.engine(rows, max_per_pack=0)
        self.assertEqual(len(e.search("wolf howling")), 50)
        e500 = botmod.HybridSearch(cfg(self.db, top_k=500), self.store)   # clamped to 50
        self.addCleanup(e500.close)
        self.assertEqual(len(e500.search("wolf howling")), 50)

    def test_pack_diversification(self):
        rows = [("big", f"A gray wolf howling at the moon {i}.", [], 0) for i in range(8)]
        rows += [(f"other{i}", "A gray wolf howling.", [], 0) for i in range(10)]
        e = self.engine(rows)
        first10 = [r["set_name"] for r in e.search("gray wolf howling")[:10]]
        self.assertLessEqual(first10.count("big"), 3)
        self.assertEqual(len(e.search("gray wolf howling")), 18)    # demoted, not dropped
        self.assertEqual(sum(r["set_name"] == "big" for r in e.search("gray wolf howling")), 8)
        self.store.add_pref(7, "big")
        first10 = [r["set_name"] for r in e.search("gray wolf howling", user_id=7)[:10]]
        self.assertGreater(first10.count("big"), 3)

    def test_popularity_prior(self):
        rows = [("a", "A gray wolf howling.", [], 0), ("b", "A gray wolf howling.", [], 0)]
        e = self.engine(rows, vec_weight=0.0, max_per_pack=0)
        base = [r["sid"] for r in e.search("gray wolf howling")]
        loser = base[-1]
        for uid in (1, 2, 3, 4):
            self.store.log_chosen(uid, "wolf", loser, "x")
        self.store._pop = None                                       # drop the TTL cache
        self.assertEqual(e.search("gray wolf howling")[0]["sid"], loser)

    def test_popularity_counts_distinct_users(self):
        for _ in range(20):
            self.store.log_chosen(1, "q", 5, "x")                    # one user, many clicks
        self.store.log_chosen(2, "q", 6, "x")
        self.store.log_chosen(3, "q", 6, "x")
        self.store._pop = None
        self.assertEqual(self.store.sticker_popularity(), {5: 1, 6: 2})

    def test_nsfw_soft_demotion_still_works(self):
        rows = [("a", "A wolf waving.", [], 1)] + [("b", "A wolf waving hello.", [], 0)] * 3
        e = self.engine(rows, vec_weight=0.0, max_per_pack=0)
        self.assertEqual(e.search("wolf waving")[-1]["sid"], 1)

    def test_apply_description_is_atomic_and_indexes_clean_text(self):
        e = self.engine([("a", "A wolf waving hello.", [], 0)])
        e.apply_description(1, "A red fox sits in an idle pose near a tree.")
        d = sqlite3.connect(self.db)
        self.assertIn("idle pose", d.execute("SELECT description FROM stickers").fetchone()[0])
        self.assertNotIn("idle", d.execute("SELECT description FROM fts_stickers").fetchone()[0])
        d.close()
        e.db.execute("DROP TABLE fts_stickers")                      # 2nd UPDATE will fail
        with self.assertRaises(sqlite3.OperationalError):
            e.apply_description(1, "Totally different description here.")
        self.assertIn("red fox", e.get_sticker(1)["description"])    # rolled back


class SpellFix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "s.db")
        rows = [("a", "A cheetah wearing a purple collar.", [], 0),
                ("b", "A cheetah with a red collar and a hat.", [], 0),
                ("c", "A gray wolf sleeping on a pillow.", [], 0),
                ("d", "A wolf sleeping under a tree.", [], 0)]
        make_db(self.db, rows)
        self.store = Store(str(Path(self.tmp.name) / "b.db"))
        self.e = botmod.HybridSearch(cfg(self.db, vec_weight=0.0), self.store)
        self.addCleanup(self.e.close)
        self.fix = self.e._spell.correct_query

    def test_distance(self):
        self.assertEqual(spellfix.osa("cheetha", "cheetah", 2), 1)     # transposition
        self.assertEqual(spellfix.osa("colar", "collar", 2), 1)
        self.assertEqual(spellfix.osa("abcdef", "uvwxyz", 2), 3)       # "more than maxd"

    def test_typos_fixed(self):
        self.assertEqual(self.fix("cheetha"), "cheetah")
        self.assertEqual(self.fix("cheetah colar"), "cheetah collar")
        self.assertEqual(self.fix("sleping wolf"), "sleeping wolf")

    def test_search_finds_through_typo(self):
        self.assertIn(self.e.search("cheetha collr")[0]["sid"], (1, 2))
        self.assertIn(self.e.search("sleping wolf")[0]["sid"], (3, 4))

    def test_left_alone(self):
        self.assertEqual(self.fix("cheet"), "cheet")                   # still being typed
        self.assertEqual(self.fix("cheetah"), "cheetah")               # known
        self.assertEqual(self.fix("wolf wi"), "wolf wi")               # short
        self.assertEqual(self.fix("волк спит"), "волк спит")           # not latin
        self.assertEqual(self.fix("xylophonez"), "xylophonez")         # no close word
        self.assertEqual(self.fix("sleeps"), "sleeps")                 # stem-known inflection

    def test_not_last_unfinished_word_is_a_typo(self):
        self.assertEqual(self.fix("cheeta collar"), "cheetah collar")  # not last: cannot be a prefix
        self.assertEqual(self.fix("collar cheeta"), "collar cheeta")   # last: still being typed

    def test_can_be_switched_off(self):
        e = botmod.HybridSearch(cfg(self.db, vec_weight=0.0, no_spellfix=True), self.store)
        self.addCleanup(e.close)
        self.assertEqual(e.search("cheetha"), [])


class DmFlows(unittest.TestCase):

    def setUp(self):
        import dm
        import texts
        self.dm, self.T = dm, texts
        dm._modes.clear()
        dm._cards.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "s.db")
        make_db(self.db, [("wolfpack", "A gray wolf waving hello.", [], 0),
                          ("wolfpack", "A <b>wolf</b> & friends.", [], 0)])
        self.store = Store(str(Path(self.tmp.name) / "b.db"))
        self.engine = botmod.HybridSearch(cfg(self.db), self.store)
        self.addCleanup(self.engine.close)
        self.cfg = types.SimpleNamespace(auto_approve_packs=False)
        self.sets = {"newpack": ("NewPack", "New Pack Title", "regular", 12),
                     "emojis": ("Emojis", "Emoji pack", "custom_emoji", 5)}
        self.bot = self.FakeBot(self.sets)

    class FakeBot:
        id = 1

        def __init__(self, sets):
            self.sets, self.fail = sets, False

        async def me(self):
            return types.SimpleNamespace(username="snoots_bot")

        async def get_sticker_set(self, name):
            from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
            from aiogram.methods import GetStickerSet
            if self.fail:
                raise TelegramAPIError(method=GetStickerSet(name=name), message="boom")
            if name.lower() not in self.sets:
                raise TelegramBadRequest(method=GetStickerSet(name=name),
                                         message="STICKERSET_INVALID")
            n, title, kind, count = self.sets[name.lower()]
            return types.SimpleNamespace(name=n, title=title, sticker_type=kind,
                                         stickers=[object()] * count)

    class Msg:
        def __init__(self, uid=1, text=None, sticker=None):
            self.from_user = types.SimpleNamespace(id=uid)
            self.chat = types.SimpleNamespace(id=uid)
            self.text, self.sticker, self.sent = text, sticker, []

        async def answer(self, text, parse_mode=None, reply_markup=None):
            self.sent.append((text, reply_markup))

        async def edit_text(self, text, parse_mode=None, reply_markup=None):
            self.sent.append((text, reply_markup))

        async def edit_reply_markup(self, reply_markup=None):
            self.sent.append((None, reply_markup))

    class Cb:
        def __init__(self, data, uid=1, message=None):
            self.data, self.from_user, self.message = data, types.SimpleNamespace(id=uid), message
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append((text, show_alert))

    def go(self, coro):
        return asyncio.run(coro)

    @staticmethod
    def buttons(kb):
        return [b for row in kb.inline_keyboard for b in row]

    # ---------------------------------------------------------------- the voice
    def test_no_text_mentions_moderation(self):
        bad = re.compile(r"moderat|review|approv|admin|queue|owner|verif", re.I)
        for name, text in self.T.all_texts().items():
            self.assertIsNone(bad.search(text), f"{name} talks about moderation: {text!r}")

    def test_texts_are_valid_telegram_html(self):
        for name, text in self.T.all_texts().items():
            stripped = re.sub(r"</?(b|i|code)>", "", text)
            self.assertNotRegex(stripped, r"<[^>]*>", name)             # no stray tags
            for tag in ("b", "i", "code"):
                self.assertEqual(text.count(f"<{tag}>"), text.count(f"</{tag}>"), name)

    def test_texts_have_emoji_and_format_cleanly(self):
        self.assertIn("🐾", self.T.START)
        self.T.START.format(bot="x")
        self.T.PACK_ADDED.format(title="t", count=1)
        self.T.CARD_KNOWN.format(sid=1, pack="p", description="d")

    # ---------------------------------------------------------- adding a pack
    def test_add_known_pack_just_favorites_it(self):
        res = self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1, "WOLFPACK"))
        self.assertEqual(res["status"], "indexed")
        self.assertEqual(self.store.user_prefs(1), {"wolfpack"})       # canonical name
        self.assertEqual(self.store.pack_requests(), [])

    def test_add_new_pack_queues_it_once_for_many_users(self):
        for uid in (1, 2):
            res = self.go(self.dm.add_pack(self.bot, self.store, self.engine, uid, "newpack"))
            self.assertEqual(res["status"], "queued")
        rows = self.store.pack_requests()
        self.assertEqual([(r["pack"], r["users"], r["status"], r["count"]) for r in rows],
                         [("NewPack", 2, "pending", 12)])
        self.assertEqual(self.store.user_prefs(2), {"NewPack"})
        text = self.dm.add_result_text(res)
        self.assertIn("New Pack Title", text)

    def test_auto_approve_flag(self):
        self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1, "newpack", True))
        self.assertEqual(self.store.pack_requests()[0]["status"], "approved")

    def test_bad_packs_store_nothing(self):
        for name, status in (("nope_nope", "not_found"), ("emojis", "not_stickers")):
            self.assertEqual(self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1,
                                                       name))["status"], status)
        self.bot.fail = True
        self.assertEqual(self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1,
                                                   "newpack"))["status"], "tg_error")
        self.assertEqual((self.store.pack_requests(), self.store.user_prefs(1)), ([], set()))

    def test_daily_limit_is_silent_and_per_user(self):
        for i in range(self.dm.MAX_PACK_REQUESTS_PER_DAY):
            self.sets[f"pack{i}"] = (f"Pack{i}", "t", "regular", 1)
            self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1, f"pack{i}"))
        self.sets["onemore"] = ("OneMore", "t", "regular", 1)
        self.assertEqual(self.go(self.dm.add_pack(self.bot, self.store, self.engine, 1,
                                                   "onemore"))["status"], "limit")
        self.assertEqual(self.go(self.dm.add_pack(self.bot, self.store, self.engine, 2,
                                                   "onemore"))["status"], "queued")

    def test_link_works_without_any_mode(self):
        m = self.Msg(text="look t.me/addstickers/NewPack nice!")
        self.go(self.dm.h_text(m, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual(self.store.pack_requests()[0]["pack"], "NewPack")
        self.assertIn("added", m.sent[0][0])

    def test_short_name_needs_the_add_prompt_and_bad_name_keeps_it_armed(self):
        m = self.Msg(text="newpack")
        self.go(self.dm.h_text(m, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual(self.store.pack_requests(), [])                # plain text = chit-chat
        self.assertIn("Boop", m.sent[0][0])
        self.dm._set_mode(1, "add_pack")
        bad = self.Msg(text="not a name!")
        self.go(self.dm.h_text(bad, self.bot, self.store, self.engine, self.cfg))
        self.assertIsNotNone(self.dm._get_mode(1))                      # still waiting
        ok = self.Msg(text="@newpack")
        self.go(self.dm.h_text(ok, self.bot, self.store, self.engine, self.cfg))
        self.assertIsNone(self.dm._get_mode(1))
        self.assertEqual(len(self.store.pack_requests()), 1)

    def test_prompt_expires(self):
        self.dm._set_mode(1, "add_pack")
        kind, payload, ts = self.dm._modes[1]
        self.dm._modes[1] = (kind, payload, ts - self.dm.MODE_TTL - 1)
        self.assertIsNone(self.dm._get_mode(1))

    # ------------------------------------------------------------- favorites
    def test_favorites_view_has_open_and_delete_buttons(self):
        long = "L" * 64
        for p in ("beta", "Alpha", long):
            self.store.add_pref(1, p)
        text, kb = self.go(self.dm.fav_view(self.store, 1, 0))
        btns = self.buttons(kb.as_markup())
        self.assertEqual([b.text for b in btns if b.url], ["🐾 Alpha", "🐾 beta", "🐾 " + long])
        dels = [b for b in btns if b.callback_data and b.callback_data.startswith("unfav:")]
        self.assertEqual(len(dels), 3)
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for b in btns if b.callback_data))
        self.assertNotIn("empty", text)

    def test_delete_really_removes_and_redraws(self):
        self.store.add_pref(1, "Alpha")
        self.store.add_pref(1, "beta")
        _, kb = self.go(self.dm.fav_view(self.store, 1, 0))
        key = next(b.callback_data for b in self.buttons(kb.as_markup())
                   if (b.callback_data or "").startswith("unfav:"))
        msg = self.Msg()
        cb = self.Cb(key, message=msg)
        self.go(self.dm.cb_unfav(cb, self.store))
        self.assertEqual(self.store.user_prefs(1), {"beta"})            # gone from the list
        self.assertIn("Alpha", cb.answers[0][0])
        redraw = [b.text for b in self.buttons(msg.sent[-1][1]) if b.url]
        self.assertEqual(redraw, ["🐾 beta"])
        self.go(self.dm.cb_unfav(self.Cb(key, message=self.Msg()), self.store))   # stale: no crash

    def test_empty_favorites_invites_to_add(self):
        text, kb = self.go(self.dm.fav_view(self.store, 1, 0))
        self.assertIn("empty", text)
        self.assertEqual([b.callback_data for b in self.buttons(kb.as_markup())],
                         ["m:add", "m:pop:0"])

    def test_pagination_and_page_clamp(self):
        for i in range(self.dm.PACKS_PAGE + 2):
            self.store.add_pref(1, f"pack{i:02d}")
        _, kb = self.go(self.dm.fav_view(self.store, 1, 0))
        self.assertIn("m:fav:1", [b.callback_data for b in self.buttons(kb.as_markup())])
        text, kb = self.go(self.dm.fav_view(self.store, 1, 99))        # overshoot -> last page
        self.assertEqual(len([b for b in self.buttons(kb.as_markup()) if b.url]), 2)

    def test_popular_excludes_favorites_and_adds_on_tap(self):
        for uid, pack in ((2, "Hot"), (3, "Hot"), (2, "Cold"), (1, "Mine")):
            self.store.log_chosen(uid, "q", 1, pack)
        self.store.add_pref(1, "Mine")
        names, more = self.store.popular_packs(1)
        self.assertEqual((names, more), (["Hot", "Cold"], False))
        _, kb = self.go(self.dm.popular_view(self.store, 1, 0))
        tap = next(b.callback_data for b in self.buttons(kb.as_markup())
                   if (b.callback_data or "").startswith("fav:"))
        msg = self.Msg()
        self.go(self.dm.cb_fav(self.Cb(tap, message=msg), self.store))
        self.assertIn(self.store.list_prefs(1)[0], {"Hot", "Mine", "Cold"})
        self.assertEqual(self.store.popular_packs(1)[0], ["Cold"])

    def test_favorites_cap(self):
        from store import MAX_PREFS
        for i in range(MAX_PREFS):
            self.assertTrue(self.store.add_pref(1, f"pack{i}"))
        self.assertFalse(self.store.add_pref(1, "one_too_many"))
        self.assertTrue(self.store.add_pref(1, "PACK0"))                # already there
        self.assertTrue(self.store.remove_pref(1, "PACK1"))             # case-insensitive
        self.assertTrue(self.store.add_pref(1, "one_too_many"))

    # --------------------------------------------------------- sticker cards
    def sticker(self, set_name="wolfpack", uid="u1"):
        return types.SimpleNamespace(set_name=set_name, file_unique_id=uid)

    def test_card_for_known_sticker_escapes_html_and_offers_actions(self):
        m = self.Msg(sticker=self.sticker(uid="u2"))
        self.go(self.dm.h_sticker(m, self.bot, self.store, self.engine, self.cfg))
        text, kb = m.sent[0]
        self.assertIn("&lt;b&gt;wolf&lt;/b&gt; &amp; friends", text)    # user-ish text is escaped
        self.assertEqual(sorted(b.callback_data for b in self.buttons(kb)),
                         ["card:edit:2", "card:fav", "m:fav:0"])

    def test_card_buttons_toggle_the_pack(self):
        m = self.Msg(sticker=self.sticker())
        self.go(self.dm.h_sticker(m, self.bot, self.store, self.engine, self.cfg))
        msg = self.Msg()
        self.go(self.dm.cb_card(self.Cb("card:fav", message=msg), self.bot, self.store,
                                 self.engine, self.cfg))
        self.assertEqual(self.store.user_prefs(1), {"wolfpack"})
        self.assertIn("card:unfav", [b.callback_data for b in self.buttons(msg.sent[-1][1])])
        self.go(self.dm.cb_card(self.Cb("card:unfav", message=msg), self.bot, self.store,
                                 self.engine, self.cfg))
        self.assertEqual(self.store.user_prefs(1), set())

    def test_card_of_unknown_pack_can_add_it(self):
        m = self.Msg(sticker=self.sticker("NewPack", "zzz"))
        self.go(self.dm.h_sticker(m, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual([b.callback_data for b in self.buttons(m.sent[0][1])][0],
                         "card:add")
        msg = self.Msg()
        self.go(self.dm.cb_card(self.Cb("card:add", message=msg), self.bot, self.store,
                                 self.engine, self.cfg))
        self.assertEqual(self.store.pack_requests()[0]["pack"], "NewPack")

    def test_sticker_in_add_mode_adds_its_pack(self):
        self.dm._set_mode(1, "add_pack")
        m = self.Msg(sticker=self.sticker("NewPack", "zzz"))
        self.go(self.dm.h_sticker(m, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual(self.store.pack_requests()[0]["pack"], "NewPack")
        self.assertIsNone(self.dm._get_mode(1))

    def test_sticker_without_a_pack(self):
        m = self.Msg(sticker=self.sticker(set_name=None))
        self.go(self.dm.h_sticker(m, self.bot, self.store, self.engine, self.cfg))
        self.assertIn("doesn't belong to a pack", m.sent[0][0])

    def test_description_flow_saves_a_suggestion_without_promises(self):
        cb = self.Cb("card:edit:1", message=self.Msg())
        self.go(self.dm.cb_card(cb, self.bot, self.store, self.engine, self.cfg))
        short = self.Msg(text="too short")
        self.go(self.dm.h_text(short, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual(self.store.pending_suggestions(), [])
        good = self.Msg(text="A gray wolf waving hello with a big smile")
        self.go(self.dm.h_text(good, self.bot, self.store, self.engine, self.cfg))
        self.assertEqual(self.store.pending_suggestions()[0]["text"],
                         "A gray wolf waving hello with a big smile")
        self.assertIn("Thank you", good.sent[0][0])

    def test_cancel(self):
        self.dm._set_mode(1, "add_pack")
        m = self.Msg()
        self.go(self.dm.h_cancel(m))
        self.assertIn("cancelled", m.sent[0][0])
        again = self.Msg()
        self.go(self.dm.h_cancel(again))
        self.assertIn("Nothing to cancel", again.sent[0][0])

    def test_old_buttons_get_a_friendly_answer(self):
        cb = self.Cb("pref:0:somepack")
        self.go(self.dm.cb_legacy(cb))
        self.assertTrue(cb.answers[0][1])

    def test_start_names_the_bot(self):
        m = self.Msg()
        self.go(self.dm.h_start(m, self.bot))
        self.assertIn("@snoots_bot", m.sent[0][0])


class ParserRequests(unittest.TestCase):
    def test_only_approved_requests_reach_the_parser(self):
        import parser as parser_mod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "bot_data.db")
        store = Store(path)
        for pack in ("AaaPack", "BbbPack", "CccPack"):
            store.add_pack_request(1, pack, pack, 3)
        store.decide_pack_request("AaaPack", True)
        store.decide_pack_request("BbbPack", False)
        self.assertEqual(parser_mod.approved_requests(path), ["AaaPack"])
        self.assertEqual(parser_mod.approved_requests(str(Path(tmp.name) / "missing.db")), [])
        self.assertEqual(parser_mod.approved_requests(""), [])


class PanelServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import socket
        import time
        import panel
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.thread = panel.start_panel(object(), object(), "127.0.0.1", cls.port, "pässword")
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    def post_login(self, pw):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/login",
                                     data=json.dumps({"password": pw}).encode(), method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def test_1_thread_survives_and_serves(self):
        import urllib.request
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/me").status, 200)

    def test_2_non_ascii_password_is_401_not_500(self):
        self.assertEqual(self.post_login("пароль")[0], 401)
        self.assertEqual(self.post_login("pässword")[0], 200)            # non-ASCII right one works

    def test_3_login_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.assertEqual(self.post_login("wrong")[0], 401)
        code, headers = self.post_login("pässword")                      # right password, locked
        self.assertEqual(code, 429)
        self.assertTrue(int(headers["Retry-After"]) > 0)


class SearchLoggerTests(unittest.TestCase):
    class Rec:
        def __init__(self):
            self.rows = []

        def log_search(self, uid, text):
            self.rows.append((uid, text))

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_keystrokes_collapse_to_one_event(self):
        async def go():
            rec = self.Rec()
            lg = botmod.SearchLogger(rec, 0.05)
            for t in ("wo", "wol", "wolf", "wolf g"):
                lg.push(1, t)
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            return rec.rows
        self.assertEqual(self.run_async(go()), [(1, "wolf g")])

    def test_users_are_independent_and_flush_writes_now(self):
        async def go():
            rec = self.Rec()
            lg = botmod.SearchLogger(rec, 5.0)
            lg.push(1, "cheetah")
            lg.push(2, "wolf")
            await lg.flush(1)                                        # user 1 chose a result
            before = list(rec.rows)
            await lg.close()                                         # shutdown flushes the rest
            return before, rec.rows
        before, after = self.run_async(go())
        self.assertEqual(before, [(1, "cheetah")])
        self.assertEqual(sorted(after), [(1, "cheetah"), (2, "wolf")])

    def test_settle_zero_logs_every_query(self):
        async def go():
            rec = self.Rec()
            lg = botmod.SearchLogger(rec, 0)
            lg.push(1, "a1")
            lg.push(1, "a12")
            await asyncio.sleep(0.1)
            return rec.rows
        self.assertGreaterEqual(len(self.run_async(go())), 1)

    def test_chosen_handler_maps_result_id_to_sticker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "s.db")
        make_db(db, [("a", "A wolf waving.", [], 0), ("packb", "A fox waving.", [], 0)])
        store = Store(str(Path(tmp.name) / "b.db"))
        eng = botmod.HybridSearch(cfg(db), store)

        async def go():
            lg = botmod.SearchLogger(store, 5.0)
            lg.push(9, "fox waving")
            cr = types.SimpleNamespace(from_user=types.SimpleNamespace(id=9),
                                       result_id="2", query="fox waving")
            await botmod.h_chosen(cr, eng, store, lg)
            bad = types.SimpleNamespace(from_user=types.SimpleNamespace(id=9),
                                        result_id="nonsense", query="x")
            await botmod.h_chosen(bad, eng, store, lg)
        asyncio.run(go())
        rows = store.db.execute("SELECT user_id, query, sticker_id, set_name FROM chosen_events "
                                "ORDER BY id").fetchall()
        self.assertEqual(rows[0], (9, "fox waving", 2, "packb"))
        self.assertEqual(rows[1][2], None)
        self.assertEqual(store.db.execute("SELECT query FROM search_events").fetchall(),
                         [("fox waving",)])                          # search event written first
        eng.close()


class Reindex(unittest.TestCase):
    def test_rebuild_upgrades_tokenizer_and_cleans(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "old.db")
        old = ("CREATE VIRTUAL TABLE fts_stickers USING fts5(description, characters, objects, "
               "actions, emotions, visual_tags, style_tags, emoji, set_title, "
               "tokenize='unicode61 remove_diacritics 2')")
        make_db(path, [("a", "A cat sleeps in an idle pose.", ["idle pose", "yawning"], 0),
                       ("b", "A dog barking.", [], 0)], fts_sql=old)
        db = sqlite3.connect(path, isolation_level=None)
        self.assertEqual(reindex.current_tokenizer(db), "unicode61")
        self.assertEqual(db.execute("SELECT count(*) FROM fts_stickers WHERE fts_stickers "
                                    "MATCH '\"sleeping\"'").fetchone()[0], 0)
        self.assertEqual(reindex.rebuild_fts(db), 2)
        self.assertEqual(reindex.current_tokenizer(db), "porter")
        self.assertEqual(db.execute("SELECT count(*) FROM fts_stickers WHERE fts_stickers "
                                    "MATCH '\"sleeping\"'").fetchone()[0], 1)      # stemming
        self.assertEqual(db.execute("SELECT count(*) FROM fts_stickers WHERE fts_stickers "
                                    "MATCH '\"idle\"'").fetchone()[0], 0)          # filler gone
        self.assertEqual(db.execute("SELECT actions FROM fts_stickers WHERE rowid=1").fetchone()[0],
                         "yawning")
        self.assertIn("idle pose", db.execute("SELECT description FROM stickers WHERE id=1")
                      .fetchone()[0])                                              # source intact
        self.assertEqual(db.execute("SELECT count(*) FROM stickers s JOIN fts_stickers f "
                                    "ON f.rowid=s.id").fetchone()[0], 2)
        self.assertEqual(reindex.rebuild_fts(db), 2)                               # idempotent
        db.close()

    def test_reembed_uses_cleaned_text_and_keeps_dim_guard(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "e.db")
        make_db(path, [("a", "A cat sleeps in an idle pose.", [], 0)])
        db = sqlite3.connect(path, isolation_level=None)
        self.assertEqual(reindex.reembed(db, FakeEmbedder()), 1)
        vec = np.frombuffer(db.execute("SELECT emb FROM stickers").fetchone()[0], dtype=np.float32)
        np.testing.assert_allclose(vec, FakeEmbedder().query("A cat sleeps."), atol=1e-6)
        bad = FakeEmbedder()
        bad.dim = 768
        with self.assertRaises(SystemExit):
            reindex.reembed(db, bad)
        db.close()


if __name__ == "__main__":
    unittest.main()


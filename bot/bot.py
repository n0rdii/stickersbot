"""Inline sticker search bot for Telegram (FTS5 + embeddings, usage metrics, admin panel)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from aiogram import Bot, Dispatcher, Router
from aiogram.types import ChosenInlineResult, InlineQuery, InlineQueryResultCachedSticker

ROOT = Path(__file__).resolve().parent.parent        # repository root
HERE = Path(__file__).resolve().parent               # bot dir (self-sufficient for a VDS)
sys.path.insert(0, str(HERE))                        # so `import embedder` resolves inside bot/

from spellfix import SpellFixer  # noqa: E402  (bot/spellfix.py)
from dm import dm_router  # noqa: E402  (bot/dm.py: the private-chat experience)
from textclean import FTS_COLUMNS, index_description  # noqa: E402  (bot/textclean.py)

log = logging.getLogger("bot")

try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(HERE / ".env")                       # bot/.env (works both on the VDS and locally)
    load_dotenv(ROOT / "parser" / ".env")            # locally - the parser's shared .env (BOT_TOKEN)
    load_dotenv()


STOPWORDS = frozenset(
    "a an the with of in on at to and or is are was were be for from by as it its "
    "this that these those".split())

FTS_WEIGHTS = {"description": 4.0, "characters": 3.0, "objects": 1.5, "actions": 2.0,
               "emotions": 1.5, "visual_tags": 1.5, "style_tags": 1.0,
               "emoji": 0.5, "set_title": 0.5}
assert set(FTS_WEIGHTS) == set(FTS_COLUMNS), "FTS_WEIGHTS must cover every FTS column"


def build_fts_queries(text: str) -> tuple[str, str] | None:
    toks = re.findall(r"\w+", text.lower())[:8]
    if not toks:
        return None
    last = toks[-1]
    keep = [t for t in toks if t not in STOPWORDS] or toks
    prefix = keep[-1] == last
    if prefix and len(keep) > 1 and len(last) < 3:
        keep, prefix = keep[:-1], False
    parts = [f'"{t}"' for t in keep]
    if prefix:
        parts[-1] += "*"
    return " AND ".join(parts), " OR ".join(parts)


# ------------------------------------------------------------------ hybrid search
class HybridSearch:

    RRF_K = 60          # standard RRF constant smoothing the rank weights

    def __init__(self, cfg, store=None):
        self.db = sqlite3.connect(str(Path(cfg.db).resolve()), check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=5000")   # the parser may write concurrently
        self.db.isolation_level = None                # explicit short transactions
        self.top_k = max(1, min(50, cfg.top_k))       # answerInlineQuery allows 50 results
        self.fts_top, self.vec_top = cfg.fts_top, cfg.vec_top
        self.fts_weight, self.vec_weight = cfg.fts_weight, cfg.vec_weight
        self.hide_nsfw = cfg.hide_nsfw
        self.pref_boost = cfg.pref_boost
        self.max_per_pack = max(0, cfg.max_per_pack)  # 0 = no pack diversification
        self.diverse_top = max(1, cfg.diverse_top)
        self.pop_weight = max(0.0, cfg.pop_weight)    # 0 = no popularity prior
        self.nsfw_penalty = cfg.nsfw_penalty          # nsfw score multiplier = (1 - penalty)
        self.nsfw_share = cfg.nsfw_share_threshold    # >= this NSFW share in top-10 = nsfw query
        self._lock = threading.RLock()                # one connection over several threads

        rows = self.db.execute("SELECT id, emb FROM stickers").fetchall()
        if not rows:
            raise SystemExit(f"{cfg.db}: the DB is empty - run parser/parser.py first")
        self._ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        self._pos = {int(sid): i for i, sid in enumerate(self._ids)}
        self._m = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])

        from embedder import Embedder                # bot/embedder.py: the bot is self-sufficient
        self.emb = Embedder(cfg.model_dir, max_len=128, threads=os.cpu_count() or 1,
                            device=cfg.emb_device or None)
        if self._m.shape[1] != self.emb.dim:
            raise SystemExit(
                f"the DB holds embeddings of dim={self._m.shape[1]}, the model gives {self.emb.dim} - "
                f"the DB is not from this model; refresh the DB or point --model-dir at that model")
        self.store = store                # metrics & pack prefs (may be None: plain search)

        fts_cols = [r[1] for r in self.db.execute("PRAGMA table_info(fts_stickers)")]
        self._bm25 = ", ".join(repr(float(FTS_WEIGHTS.get(c, 1.0))) for c in fts_cols)
        fts_sql = (self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='fts_stickers'").fetchone() or [""])[0] or ""
        if "porter" not in fts_sql.lower():
            log.warning("fts_stickers has no stemming (old DB): run `python parser/reindex.py` "
                        "once, with the bot stopped")
        self._spell = None if getattr(cfg, "no_spellfix", False) else self._build_spellfixer()
        log.info("loaded %d stickers, dim=%d, model=%s, spellfix=%s",
                 len(rows), self.emb.dim, self.emb.model_id,
                 f"{len(self._spell.words)} words" if self._spell else "off")

    # ---------------------------------------------------------------- search
    def _fts_known(self, tok: str, prefix: bool) -> bool:
        q = f'"{tok}"*' if prefix else f'"{tok}"'
        try:
            return self.db.execute(
                "SELECT 1 FROM fts_stickers WHERE fts_stickers MATCH ? LIMIT 1", (q,)
            ).fetchone() is not None
        except sqlite3.OperationalError:
            return True                       # when in doubt do not "fix" anything

    def _build_spellfixer(self) -> SpellFixer:
        words: Counter = Counter()
        for desc, *lists in self.db.execute(
                "SELECT description, characters, objects, actions, emotions, visual_tags, "
                "style_tags FROM stickers"):
            parts = [index_description(desc or "")]
            for raw in lists:
                try:
                    parts.extend(json.loads(raw or "[]"))
                except ValueError:
                    pass
            words.update(re.findall(r"[a-z0-9]+", " ".join(p for p in parts if isinstance(p, str)).lower()))
        return SpellFixer(words, self._fts_known)

    def _fts_ranked(self, text: str) -> list[int]:
        if self._spell:
            fixed = self._spell.correct_query(text)       # FTS only; dense keeps the original
            if fixed != text:
                log.debug("spellfix: %r -> %r", text, fixed)
                text = fixed
        qs = build_fts_queries(text)
        if not qs:
            return []
        and_q, or_q = qs
        sql = ("SELECT rowid FROM fts_stickers WHERE fts_stickers MATCH ? "
               f"ORDER BY bm25(fts_stickers, {self._bm25}) LIMIT ?")
        try:
            ranked = [r[0] for r in self.db.execute(sql, (and_q, self.fts_top))]
            if or_q != and_q and len(ranked) < self.fts_top:
                seen = set(ranked)
                for (sid,) in self.db.execute(sql, (or_q, self.fts_top)):
                    if sid not in seen:
                        ranked.append(sid)
                        seen.add(sid)
                        if len(ranked) >= self.fts_top:
                            break
            return ranked
        except sqlite3.OperationalError:
            log.exception("FTS query failed for %r - dense channel only", text)
            return []

    def search(self, text: str, user_id: int = 0) -> list[dict]:
        prefs = (self.store.user_prefs(user_id) if self.store and user_id else set()) \
            if self.store else set()
        prefs_fold = {p.casefold() for p in prefs}   # match pack names case-insensitively
        boosts = self.store.global_boosts() if self.store else {}   # {pack.casefold(): weight}
        pop = self.store.sticker_popularity() if self.store and self.pop_weight else {}
        qvec = self.emb.query(text)                  # encode BEFORE taking the lock
        with self._lock:
            scores: dict[int, float] = defaultdict(float)

            for rank, sid in enumerate(self._fts_ranked(text)):
                scores[sid] += self.fts_weight / (self.RRF_K + rank)

            sims = self._m @ qvec
            kth = min(self.vec_top, sims.size) - 1
            if kth >= 0:
                idx = np.argpartition(-sims, kth)[: self.vec_top]
                idx = idx[np.argsort(-sims[idx])]
                for rank, i in enumerate(idx):
                    if float(sims[i]) < 0.05:        # beyond the cutoff - noise
                        break
                    scores[int(self._ids[i])] += self.vec_weight / (self.RRF_K + rank)

            if not scores:
                return []
            order = [sid for sid, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
            marks = ",".join("?" * len(order))
            rows = {r[0]: r for r in self.db.execute(
                f"SELECT id, file_id, emoji, set_title, nsfw, set_name "
                f"FROM stickers WHERE id IN ({marks})", order)}

            top = [sid for sid in order if sid in rows][:10]
            nsfw_intent = bool(top) and \
                sum(1 for sid in top if rows[sid][4]) / len(top) >= self.nsfw_share

            adjusted = {}
            for sid in order:
                sc = scores[sid]
                r = rows.get(sid)
                if r is None:
                    continue
                if r[4] and not nsfw_intent:
                    sc *= (1.0 - self.nsfw_penalty)  # soft demotion, not a filter
                if boosts and (w := boosts.get((r[5] or "").casefold())) is not None and w != 1.0:
                    sc *= w                          # global admin-boosted pack (panel)
                if prefs_fold and (r[5] or "").casefold() in prefs_fold:
                    sc *= self.pref_boost
                if pop and (n := pop.get(sid)):
                    sc *= 1.0 + self.pop_weight * math.log1p(n)   # users picked it: nudge up
                adjusted[sid] = sc
            final = sorted(adjusted, key=lambda sid: -adjusted[sid])

            exempt = prefs_fold | set(boosts)
            head: list[dict] = []
            rest: list[dict] = []
            per_pack: Counter = Counter()
            for sid in final:
                r = rows.get(sid)
                if r is None or (self.hide_nsfw and r[4]):
                    continue
                item = {"sid": sid, "file_id": r[1], "emoji": r[2] or "",
                        "set_title": r[3] or "", "set_name": r[5] or "",
                        "nsfw": bool(r[4])}
                pk = item["set_name"].casefold()
                capped = (self.max_per_pack and pk and pk not in exempt
                          and per_pack[pk] >= self.max_per_pack)
                if len(head) < self.diverse_top and not capped:
                    head.append(item)
                    per_pack[pk] += 1
                else:
                    rest.append(item)
            return (head + rest)[: self.top_k]

    # ------------------------------------------------- description editing (admin / approved)
    def apply_description(self, sid: int, text: str, editor: str = "panel") -> str:
        text = " ".join(str(text).split())[:1000]
        if len(text) < 10:
            raise ValueError("description too short (min 10 characters)")
        indexed = index_description(text) or text    # FTS + embedding see the cleaned text
        vec = self.emb.query(indexed)                # encode outside the lock
        with self._lock:
            row = self.db.execute("SELECT description FROM stickers WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError(f"sticker id={sid} is not in the index")
            old = row[0]
            self.db.execute("BEGIN IMMEDIATE")   # stickers row + FTS row update together
            try:
                self.db.execute(
                    "UPDATE stickers SET description=?, emb=? WHERE id=?",
                    (text, vec.tobytes(), sid))
                self.db.execute(
                    "UPDATE fts_stickers SET description=? WHERE rowid=?", (indexed, sid))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            i = self._pos.get(int(sid))
            if i is not None:
                self._m[i] = vec
        log.info("description updated: sid=%d editor=%s (%d -> %d chars)",
                 sid, editor, len(old), len(text))
        return old

    def find_by_unique_id(self, file_unique_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT id, set_name, emoji, description FROM stickers "
                "WHERE file_unique_id=?", (file_unique_id,)).fetchone()
        if row is None:
            return None
        return {"sid": row[0], "set_name": row[1] or "", "emoji": row[2] or "",
                "description": row[3] or ""}

    def get_sticker(self, sid: int) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT id, set_name, emoji, description FROM stickers WHERE id=?",
                (sid,)).fetchone()
        if row is None:
            return None
        return {"sid": row[0], "set_name": row[1] or "", "emoji": row[2] or "",
                "description": row[3] or ""}

    def pack_info(self, name: str) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT set_name, set_title, COUNT(*) FROM stickers "
                "WHERE set_name=? COLLATE NOCASE", (name,)).fetchone()
        if not row or not row[2]:
            return None
        return {"name": row[0], "title": row[1] or row[0], "count": row[2]}

    def set_names(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self.db.execute(
                "SELECT DISTINCT set_name FROM stickers WHERE set_name<>''")}

    def close(self):
        with self._lock:
            self.db.close()


# ------------------------------------------------------------------ telegram
router = Router()


class SearchLogger:

    def __init__(self, store, settle: float):
        self.store, self.settle = store, settle
        self._pending: dict[int, tuple[str, asyncio.Task]] = {}

    def push(self, user_id: int, text: str) -> None:
        old = self._pending.pop(user_id, None)
        if old:
            old[1].cancel()                       # superseded by a newer keystroke
        task = asyncio.get_running_loop().create_task(self._later(user_id, text))
        self._pending[user_id] = (text, task)

    async def _later(self, user_id: int, text: str) -> None:
        if self.settle > 0:
            await asyncio.sleep(self.settle)
        cur = self._pending.get(user_id)
        if cur and cur[1] is asyncio.current_task():
            del self._pending[user_id]            # from here on it can no longer be cancelled
        await self._write(user_id, text)

    async def _write(self, user_id: int, text: str) -> None:
        try:
            await asyncio.to_thread(self.store.log_search, user_id, text)
        except Exception:
            log.exception("failed to log the search event")

    async def flush(self, user_id: int) -> None:
        old = self._pending.pop(user_id, None)
        if old:
            old[1].cancel()
            await self._write(user_id, old[0])

    async def close(self) -> None:
        for uid in list(self._pending):
            await self.flush(uid)


@router.inline_query()
async def h_inline(q: InlineQuery, engine: HybridSearch, cfg, search_log: SearchLogger):
    text = (q.query or "").strip()
    if len(text) < 2:                                # hasn't started typing yet
        await q.answer(results=[], cache_time=1, is_personal=False)
        return
    try:
        results = await asyncio.to_thread(engine.search, text, q.from_user.id)
    except Exception:
        log.exception("search for %r failed", text)
        await q.answer(results=[], cache_time=1, is_personal=False)
        return
    search_log.push(q.from_user.id, text)            # logged once the user stops typing
    if not results:
        await q.answer(results=[], cache_time=cfg.cache_time, is_personal=False)
        return
    items = [InlineQueryResultCachedSticker(
        id=str(r["sid"]), sticker_file_id=r["file_id"]) for r in results]
    # result id = stickers.id: ChosenInlineResult maps back even with Telegram's own caching
    await q.answer(results=items, cache_time=cfg.cache_time, is_personal=True)


@router.chosen_inline_result()
async def h_chosen(cr: ChosenInlineResult, engine: HybridSearch, store,
                   search_log: SearchLogger):
    await search_log.flush(cr.from_user.id)          # the search event goes first
    sid, set_name = None, None
    try:
        info = await asyncio.to_thread(engine.get_sticker, int(cr.result_id))
    except (ValueError, TypeError):
        info = None
    if info is not None:
        sid, set_name = info["sid"], info["set_name"]
    try:
        await asyncio.to_thread(store.log_chosen, cr.from_user.id, cr.query, sid, set_name)
    except Exception:
        log.exception("failed to log the chosen event")


async def main(cfg):
    from store import Store                     # bot/store.py

    store = await asyncio.to_thread(Store, cfg.prefs_db)
    engine = await asyncio.to_thread(HybridSearch, cfg, store)

    if not cfg.no_panel and os.getenv("PANEL_PASSWORD"):
        from panel import start_panel
        if start_panel(engine, store, cfg.panel_host, cfg.panel_port,
                       os.environ["PANEL_PASSWORD"]):
            log.info("admin panel: http://%s:%d", cfg.panel_host, cfg.panel_port)
        else:
            log.warning("admin panel disabled: aiohttp is not installed")
    elif not os.getenv("PANEL_PASSWORD"):
        log.info("PANEL_PASSWORD is not set - admin panel disabled")

    dp = Dispatcher()
    search_log = SearchLogger(store, cfg.log_settle)
    dp["engine"], dp["store"], dp["cfg"], dp["search_log"] = engine, store, cfg, search_log
    dp.include_router(router)
    dp.include_router(dm_router)
    bot = Bot(os.environ["BOT_TOKEN"])
    log.info("bot is up: db=%s top_k=%d (fts x %.1f + vec x %.1f) pref_boost=%.2f "
             "nsfw_penalty=%.2f", cfg.db, cfg.top_k, cfg.fts_weight, cfg.vec_weight,
             cfg.pref_boost, cfg.nsfw_penalty)
    try:
        await dp.start_polling(bot)
    finally:
        await search_log.close()                     # flush queries still waiting to settle
        await bot.session.close()
        await asyncio.to_thread(engine.close)


def parse_args(argv=None):
    here = Path(__file__).resolve().parent
    root = here.parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(root / "data" / "stickers.db"))
    ap.add_argument("--prefs-db", default=str(root / "data" / "bot_data.db"),
                    help="sqlite for metrics/prefs/suggestions (bot_data.db)")
    ap.add_argument("--model-dir",
                    default=os.getenv("MODEL_DIR") or str(here / "model_multilingual_minilm"))
    ap.add_argument("--emb-device", default=os.getenv("EMB_DEVICE", ""),
                    help="cuda/cpu for the embedder; empty = auto")
    ap.add_argument("--top-k", type=int, default=50,
                    help="how many stickers to answer with (Telegram allows at most 50)")
    ap.add_argument("--fts-top", type=int, default=300, help="candidates from the FTS channel")
    ap.add_argument("--vec-top", type=int, default=300, help="candidates from the vector channel")
    ap.add_argument("--fts-weight", type=float, default=1.0)
    ap.add_argument("--vec-weight", type=float, default=1.5,
                    help="dense channel weight in RRF (>1 favours meaning over keywords)")
    ap.add_argument("--cache-time", type=int, default=60,
                    help="Telegram cache for identical queries, seconds")
    ap.add_argument("--hide-nsfw", action="store_true",
                    help="do not show stickers with nsfw=1 at all")
    ap.add_argument("--nsfw-penalty", type=float, default=0.5,
                    help="0..1: NSFW score multiplier = 1 - penalty (soft demotion)")
    ap.add_argument("--nsfw-share-threshold", type=float, default=0.5,
                    help="if this fraction of the top-10 candidates is NSFW, the query "
                         "counts as NSFW-intent and no penalty applies")
    ap.add_argument("--pref-boost", type=float, default=2,
                    help="score multiplier for stickers from the user's preferred packs")
    ap.add_argument("--max-per-pack", type=int, default=3,
                    help="max stickers of one pack among the first --diverse-top results "
                         "(0 = off; preferred/boosted packs are exempt)")
    ap.add_argument("--diverse-top", type=int, default=10,
                    help="how many leading results the per-pack cap applies to")
    ap.add_argument("--pop-weight", type=float, default=0.05,
                    help="popularity prior: score x (1 + w*ln(1+distinct users who picked "
                         "the sticker)); 0 = off")
    ap.add_argument("--log-settle", type=float, default=1.5,
                    help="log a search once the user stopped typing for this many seconds "
                         "(0 = log every keystroke)")
    ap.add_argument("--auto-approve-packs", action="store_true",
                    default=os.getenv("AUTO_APPROVE_PACKS", "").lower() in ("1", "true", "yes"),
                    help="packs users add go straight to the indexing list "
                         "(default: they wait for a click in the panel's packs tab)")
    ap.add_argument("--no-spellfix", action="store_true",
                    help="do not correct typos of query words unknown to the index")
    ap.add_argument("--no-panel", action="store_true", help="do not start the admin panel")
    ap.add_argument("--panel-host", default=os.getenv("PANEL_HOST", "127.0.0.1"))
    ap.add_argument("--panel-port", type=int, default=int(os.getenv("PANEL_PORT", "8081")))
    return ap.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not os.getenv("BOT_TOKEN"):
        sys.exit("BOT_TOKEN is not set (put it in .env next to parser.py)")
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        print("bot stopped")

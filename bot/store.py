"""Writable bot data: usage metrics, pack preferences, pack requests, description suggestions."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time

DAY = 86400
MAX_PREFS = 100                    # favorite packs per user
POP_TTL = 60                       # seconds the popularity map is reused

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_users_first ON users(first_seen);

CREATE TABLE IF NOT EXISTS search_events(
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  query TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_search_ts ON search_events(ts);
CREATE INDEX IF NOT EXISTS idx_search_user ON search_events(user_id);

CREATE TABLE IF NOT EXISTS chosen_events(
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  user_id INTEGER,
  query TEXT,
  sticker_id INTEGER,
  set_name TEXT);
CREATE INDEX IF NOT EXISTS idx_chosen_ts ON chosen_events(ts);
CREATE INDEX IF NOT EXISTS idx_chosen_sticker ON chosen_events(sticker_id, user_id);
CREATE INDEX IF NOT EXISTS idx_chosen_set ON chosen_events(set_name);

CREATE TABLE IF NOT EXISTS pack_prefs(
  user_id INTEGER NOT NULL,
  pack TEXT NOT NULL,
  PRIMARY KEY(user_id, pack));

CREATE TABLE IF NOT EXISTS pack_boosts(
  pack TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,   -- short set name, case-insensitive
  weight REAL NOT NULL DEFAULT 2.0,                -- ranking multiplier for every user
  note TEXT NOT NULL DEFAULT '',                   -- e.g. "pack of a friend"
  created_ts INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS pack_requests(      -- packs users asked to add to the search
  pack TEXT NOT NULL PRIMARY KEY COLLATE NOCASE,
  title TEXT NOT NULL DEFAULT '',
  sticker_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | rejected
  created_ts INTEGER NOT NULL,
  decided_ts INTEGER);
CREATE INDEX IF NOT EXISTS idx_prq_status ON pack_requests(status);

CREATE TABLE IF NOT EXISTS pack_request_users(
  pack TEXT NOT NULL COLLATE NOCASE,
  user_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  PRIMARY KEY(pack, user_id));
CREATE INDEX IF NOT EXISTS idx_prusers_user ON pack_request_users(user_id, ts);

CREATE TABLE IF NOT EXISTS edit_suggestions(
  id INTEGER PRIMARY KEY,
  sticker_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
  created_ts INTEGER NOT NULL,
  decided_ts INTEGER);
CREATE INDEX IF NOT EXISTS idx_sugg_status ON edit_suggestions(status);

CREATE TABLE IF NOT EXISTS edits_log(
  id INTEGER PRIMARY KEY,
  sticker_id INTEGER NOT NULL,
  editor TEXT NOT NULL,                       -- 'panel' or 'user:<tg_id>'
  ts INTEGER NOT NULL,
  old_text TEXT NOT NULL,
  new_text TEXT NOT NULL);
"""


def pack_key(pack: str) -> str:
    return hashlib.sha1(pack.encode()).hexdigest()[:12]


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()          # one connection works for many threads
        self._pop: dict[int, int] | None = None  # popularity cache, see sticker_popularity()
        self._pop_ts = 0.0
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ------------------------------------------------------------ events
    def touch_user(self, user_id: int):
        now = int(time.time())
        self.db.execute(
            "INSERT INTO users(user_id,first_seen,last_seen) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
            (user_id, now, now))

    def log_search(self, user_id: int, query: str):
        with self._lock:
            self.touch_user(user_id)
            self.db.execute(
                "INSERT INTO search_events(ts,user_id,query) VALUES(?,?,?)",
                (int(time.time()), user_id, query[:200]))
            self.db.commit()

    def log_chosen(self, user_id: int, query: str, sticker_id: int | None, set_name: str):
        with self._lock:
            self.touch_user(user_id)
            self.db.execute(
                "INSERT INTO chosen_events(ts,user_id,query,sticker_id,set_name) "
                "VALUES(?,?,?,?,?)",
                (int(time.time()), user_id, (query or "")[:200], sticker_id, set_name or ""))
            self.db.commit()

    # ------------------------------------------------------------ popularity prior
    def sticker_popularity(self) -> dict[int, int]:
        now = time.monotonic()
        with self._lock:
            if self._pop is None or now - self._pop_ts > POP_TTL:
                rows = self.db.execute(
                    "SELECT sticker_id, COUNT(DISTINCT user_id) FROM chosen_events "
                    "WHERE sticker_id IS NOT NULL GROUP BY sticker_id").fetchall()
                self._pop = {r[0]: r[1] for r in rows}
                self._pop_ts = now
            return self._pop

    # ------------------------------------------------------------ pack prefs
    def user_prefs(self, user_id: int) -> set[str]:
        with self._lock:
            rows = self.db.execute(
                "SELECT pack FROM pack_prefs WHERE user_id=?", (user_id,)).fetchall()
        return {r[0] for r in rows}

    def toggle_pref(self, user_id: int, pack: str) -> bool:
        with self._lock:
            if self.db.execute(
                    "SELECT 1 FROM pack_prefs WHERE user_id=? AND pack=?",
                    (user_id, pack)).fetchone():
                self.db.execute("DELETE FROM pack_prefs WHERE user_id=? AND pack=?",
                                (user_id, pack))
                self.db.commit()
                return False
            self.db.execute("INSERT OR IGNORE INTO pack_prefs VALUES(?,?)", (user_id, pack))
            self.db.commit()
            return True

    def pack_candidates(self, user_id: int, offset: int = 0, limit: int = 10
                        ) -> list[tuple[str, int, bool]]:
        offset, limit = max(0, offset), max(1, min(50, limit))
        with self._lock:
            need = offset + limit
            hist = self.db.execute(
                "SELECT set_name, COUNT(*) FROM chosen_events "
                "WHERE user_id=? AND set_name<>'' GROUP BY set_name "
                "ORDER BY 2 DESC LIMIT ?", (user_id, need)).fetchall()
            extra = self.db.execute(
                "SELECT set_name, COUNT(*) FROM chosen_events WHERE set_name<>'' "
                "GROUP BY set_name ORDER BY 2 DESC LIMIT ?", (need,)).fetchall()
            prefs = self.user_prefs(user_id)
        merged: dict[str, int] = {}
        for name, n in (*hist, *extra):
            merged[name] = max(merged.get(name, 0), n)
        for p in prefs:                       # a preferred pack must stay manageable
            merged.setdefault(p, 0)           # even with no chosen_events at all
        out = [(p, n, p in prefs) for p, n in merged.items()]
        out.sort(key=lambda x: (not x[2], -x[1]))
        return out[offset:offset + limit]

    def pack_by_key(self, user_id: int, key: str) -> str | None:
        with self._lock:
            names = {r[0] for r in self.db.execute(
                "SELECT DISTINCT set_name FROM chosen_events WHERE set_name<>''")}
            names |= self.user_prefs(user_id)
        return next((n for n in names if pack_key(n) == key), None)

    def add_pref(self, user_id: int, pack: str) -> bool:
        with self._lock:
            have = {p.casefold() for p in self.user_prefs(user_id)}
            if pack.casefold() in have:
                return True
            if len(have) >= MAX_PREFS:
                return False
            self.db.execute("INSERT OR IGNORE INTO pack_prefs VALUES(?,?)",
                            (user_id, pack))
            self.db.commit()
        return True

    def remove_pref(self, user_id: int, pack: str) -> bool:
        with self._lock:
            gone = self.db.execute(
                "DELETE FROM pack_prefs WHERE user_id=? AND pack=? COLLATE NOCASE",
                (user_id, pack)).rowcount
            self.db.commit()
        return bool(gone)

    def list_prefs(self, user_id: int) -> list[str]:
        return sorted(self.user_prefs(user_id), key=str.casefold)

    def popular_packs(self, user_id: int, offset: int = 0, limit: int = 8
                      ) -> tuple[list[str], bool]:
        offset, limit = max(0, offset), max(1, min(50, limit))
        with self._lock:
            rows = self.db.execute(
                "SELECT set_name, SUM(user_id=?) AS mine, COUNT(DISTINCT user_id) AS n "
                "FROM chosen_events WHERE set_name<>'' AND set_name NOT IN "
                "(SELECT pack FROM pack_prefs WHERE user_id=?) "
                "GROUP BY set_name ORDER BY mine DESC, n DESC, set_name "
                "LIMIT ? OFFSET ?", (user_id, user_id, limit + 1, offset)).fetchall()
        return [r[0] for r in rows[:limit]], len(rows) > limit

    # ------------------------------------------------------- pack requests (indexing)
    def pack_requests_today(self, user_id: int) -> int:
        with self._lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM pack_request_users WHERE user_id=? AND ts>=?",
                (user_id, int(time.time()) - DAY)).fetchone()[0]

    def add_pack_request(self, user_id: int, pack: str, title: str, count: int,
                         auto_approve: bool = False) -> bool:
        now = int(time.time())
        with self._lock:
            new = self.db.execute(
                "INSERT OR IGNORE INTO pack_requests"
                "(pack,title,sticker_count,status,created_ts,decided_ts) VALUES(?,?,?,?,?,?)",
                (pack, title[:200], int(count),
                 "approved" if auto_approve else "pending", now,
                 now if auto_approve else None)).rowcount > 0
            self.db.execute(
                "INSERT OR IGNORE INTO pack_request_users(pack,user_id,ts) VALUES(?,?,?)",
                (pack, user_id, now))
            self.db.commit()
        return new

    def pack_requests(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT r.pack, r.title, r.sticker_count, r.status, r.created_ts, "
               "(SELECT COUNT(*) FROM pack_request_users u WHERE u.pack=r.pack) "
               "FROM pack_requests r")
        args: tuple = ()
        if status:
            sql += " WHERE r.status=?"
            args = (status,)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY r.created_ts DESC", args).fetchall()
        return [{"pack": r[0], "title": r[1], "count": r[2], "status": r[3],
                 "ts": r[4], "users": r[5]} for r in rows]

    def decide_pack_request(self, pack: str, approved: bool) -> bool:
        with self._lock:
            n = self.db.execute(
                "UPDATE pack_requests SET status=?, decided_ts=? WHERE pack=?",
                ("approved" if approved else "rejected", int(time.time()), pack)).rowcount
            self.db.commit()
        return n > 0

    # ------------------------------------------------------- global pack boosts
    def global_boosts(self) -> dict[str, float]:
        with self._lock:
            rows = self.db.execute("SELECT pack, weight FROM pack_boosts").fetchall()
        return {p.casefold(): w for p, w in rows}

    def pack_boosts(self) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT b.pack, b.weight, b.note, b.created_ts, "
                "(SELECT COUNT(*) FROM chosen_events c WHERE c.set_name=b.pack) "
                "FROM pack_boosts b ORDER BY b.pack").fetchall()
        return [{"pack": r[0], "weight": r[1], "note": r[2], "ts": r[3], "chosen": r[4]}
                for r in rows]

    def upsert_boost(self, pack: str, weight: float, note: str = ""):
        with self._lock:
            self.db.execute(
                "INSERT INTO pack_boosts(pack,weight,note,created_ts) VALUES(?,?,?,?) "
                "ON CONFLICT(pack) DO UPDATE SET weight=excluded.weight, "
                "note=excluded.note",
                (pack, float(weight), note, int(time.time())))
            self.db.commit()

    def remove_boost(self, pack: str) -> bool:
        with self._lock:
            gone = self.db.execute("DELETE FROM pack_boosts WHERE pack=? COLLATE NOCASE",
                                   (pack,)).rowcount
            self.db.commit()
        return bool(gone)

    # ------------------------------------------------------------ metrics
    def stats(self) -> dict:
        now = int(time.time())
        with self._lock:
            dau = self.db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM search_events WHERE ts>=?",
                (now - now % DAY,)).fetchone()[0]
            mau = self.db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM search_events WHERE ts>=?-30*?",
                (now, DAY)).fetchone()[0]
            new30 = self.db.execute(
                "SELECT COUNT(*) FROM users WHERE first_seen>=?",
                (now - 30 * DAY,)).fetchone()[0]
            total_q = self.db.execute("SELECT COUNT(*) FROM search_events").fetchone()[0]
            total_c = self.db.execute("SELECT COUNT(*) FROM chosen_events").fetchone()[0]
            days = self.db.execute(
                "SELECT date(ts,'unixepoch') d, COUNT(*) FROM search_events WHERE ts>=? "
                "GROUP BY d ORDER BY d", (now - 14 * DAY,)).fetchall()
            chosen_days = self.db.execute(
                "SELECT date(ts,'unixepoch') d, COUNT(*) FROM chosen_events WHERE ts>=? "
                "GROUP BY d ORDER BY d", (now - 14 * DAY,)).fetchall()
            recent = self.db.execute(
                "SELECT ts, user_id, query FROM search_events ORDER BY id DESC LIMIT 20"
            ).fetchall()
        day_counts_q = {d: n for d, n in days}
        day_counts_c = {d: n for d, n in chosen_days}
        all_days = sorted(set(day_counts_q) | set(day_counts_c))[-14:]
        return {
            "dau": dau,
            "mau": mau,
            "new_users": new30,
            "new_ratio": (new30 / mau) if mau else 0.0,
            "total_queries": total_q,
            "total_chosen": total_c,
            "ctr": (total_c / total_q) if total_q else 0.0,
            "categories": all_days,
            "queries_series": [day_counts_q.get(d, 0) for d in all_days],
            "chosen_series": [day_counts_c.get(d, 0) for d in all_days],
            "recent": recent,
        }

    def search_history(self, page: int = 1, page_size: int = 50) -> dict:
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) FROM search_events").fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)                 # clamp an overshooting page
            rows = self.db.execute(
                "SELECT ts, user_id, query FROM search_events "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size)).fetchall()
        return {"total": total, "page": page, "page_size": page_size,
                "pages": pages, "rows": rows}

    # ------------------------------------------- suggestions / description edits
    def add_suggestion(self, user_id: int, sticker_id: int, text: str) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO edit_suggestions(sticker_id,user_id,text,created_ts) "
                "VALUES(?,?,?,?)",
                (sticker_id, user_id, text, int(time.time())))
            self.db.commit()
            return cur.lastrowid or 0

    def pending_suggestions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT id, sticker_id, user_id, text, created_ts FROM edit_suggestions "
                "WHERE status='pending' ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "sid": r[1], "user_id": r[2], "text": r[3], "ts": r[4]}
                for r in rows]

    def decide_suggestion(self, sug_id: int, approved: bool):
        with self._lock:
            self.db.execute(
                "UPDATE edit_suggestions SET status=?, decided_ts=? WHERE id=?",
                ("approved" if approved else "rejected", int(time.time()), sug_id))
            self.db.commit()

    def record_edit(self, sticker_id: int, editor: str, old_text: str, new_text: str):
        with self._lock:
            self.db.execute(
                "INSERT INTO edits_log(sticker_id,editor,ts,old_text,new_text) "
                "VALUES(?,?,?,?,?)",
                (sticker_id, editor, int(time.time()), old_text, new_text))
            self.db.commit()

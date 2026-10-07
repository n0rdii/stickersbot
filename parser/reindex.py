"""Rebuild the derived indexes of an existing stickers.db (FTS; --reembed also redoes vectors)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from textclean import (FTS_COLUMNS, FTS_LIST_KEYS, FTS_TOKENIZE,  # noqa: E402
                       fts_create_sql, fts_values, index_description)

log = logging.getLogger("reindex")


def current_tokenizer(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT sql FROM sqlite_master WHERE name='fts_stickers'").fetchone()
    if not row or not row[0]:
        return None
    return "porter" if "porter" in row[0].lower() else "unicode61"


def _loads(s) -> list:
    try:
        v = json.loads(s) if s else []
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def rebuild_fts(db: sqlite3.Connection) -> int:
    cols = "id, description, " + ", ".join(FTS_LIST_KEYS) + ", emoji, set_title"
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DROP TABLE IF EXISTS fts_new")
        db.execute(fts_create_sql("fts_new"))
        n = 0
        ph = ",".join("?" * (len(FTS_COLUMNS) + 1))
        for row in db.execute(f"SELECT {cols} FROM stickers ORDER BY id").fetchall():
            sid, desc = row[0], row[1]
            lists = {k: _loads(row[2 + i]) for i, k in enumerate(FTS_LIST_KEYS)}
            emoji, title = row[2 + len(FTS_LIST_KEYS)], row[3 + len(FTS_LIST_KEYS)]
            db.execute(f"INSERT INTO fts_new(rowid, {', '.join(FTS_COLUMNS)}) VALUES ({ph})",
                       (sid, *fts_values(desc or "", lists, emoji, title)))
            n += 1
        total = db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]
        if n != total:
            raise RuntimeError(f"rebuilt {n} FTS rows for {total} stickers")
        db.execute("DROP TABLE IF EXISTS fts_stickers")
        db.execute("ALTER TABLE fts_new RENAME TO fts_stickers")
        db.execute("INSERT INTO meta(key,value) VALUES('fts_tokenizer',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (FTS_TOKENIZE,))
        db.execute("INSERT INTO meta(key,value) VALUES('index_text','clean-v1') "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return n


def reembed(db: sqlite3.Connection, emb, batch: int = 64) -> int:
    row = db.execute("SELECT value FROM meta WHERE key='dim'").fetchone()
    if row and int(row[0]) != emb.dim:
        raise SystemExit(f"the DB holds dim={row[0]} vectors, the model gives {emb.dim}: "
                         f"re-embedding with another model needs a NEW DB (--db)")
    rows = db.execute("SELECT id, description FROM stickers ORDER BY id").fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vecs = emb.passages([index_description(d or "") or (d or "") for _, d in chunk],
                            batch=len(chunk))
        db.execute("BEGIN IMMEDIATE")
        try:
            for (sid, _), v in zip(chunk, vecs):
                db.execute("UPDATE stickers SET emb=? WHERE id=?", (v.tobytes(), sid))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        done += len(chunk)
        log.info("re-embedded %d/%d", done, len(rows))
    return done


def main(argv=None) -> None:
    root = HERE.parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(root / "data" / "stickers.db"))
    ap.add_argument("--reembed", action="store_true",
                    help="also recompute emb from the cleaned descriptions")
    ap.add_argument("--model-dir",
                    default=os.getenv("MODEL_DIR") or str(HERE / "model_multilingual_minilm"))
    ap.add_argument("--emb-device", default=os.getenv("EMB_DEVICE", ""))
    ap.add_argument("--no-backup", action="store_true", help="skip the .bak copy of the DB")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"{path}: no such DB")
    db = sqlite3.connect(str(path), isolation_level=None)
    db.execute("PRAGMA busy_timeout=30000")
    total = db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]
    tok = current_tokenizer(db)
    changed = sum(1 for (d,) in db.execute("SELECT description FROM stickers")
                  if index_description(d or "") != (d or ""))
    log.info("%s: %d stickers, FTS tokenizer now: %s, descriptions touched by the "
             "cleanup: %d", path, total, tok, changed)
    if args.dry_run:
        log.info("dry-run: nothing written")
        return

    if not args.no_backup:
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        dst = sqlite3.connect(str(bak))
        db.backup(dst)
        dst.close()
        log.info("backup written: %s", bak)

    n = rebuild_fts(db)
    log.info("fts_stickers rebuilt: %d rows, tokenizer '%s'", n, FTS_TOKENIZE)
    if args.reembed:
        from embedder import Embedder
        emb = Embedder(args.model_dir, max_len=128, threads=os.cpu_count() or 1,
                       device=args.emb_device or None)
        log.info("re-embedded %d stickers", reembed(db, emb))
    db.execute("VACUUM")                 # the dropped FTS table left free pages behind
    db.execute("PRAGMA optimize")
    db.close()
    log.info("done. Restart the bot so it reloads the embedding matrix.")


if __name__ == "__main__":
    main()


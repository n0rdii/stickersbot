"""tools/dbsync.py: snapshot/merge/sync logic against a folder remote."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "parser"))

import dbsync  # noqa: E402
from textclean import FTS_COLUMNS, fts_create_sql, fts_values  # noqa: E402

SCHEMA = """
CREATE TABLE stickers(id INTEGER PRIMARY KEY, file_unique_id TEXT UNIQUE NOT NULL,
  file_id TEXT NOT NULL, set_name TEXT, set_title TEXT, emoji TEXT, kind TEXT, description TEXT,
  characters TEXT, objects TEXT, actions TEXT, emotions TEXT, visual_tags TEXT, style_tags TEXT,
  nsfw INTEGER DEFAULT 0, emb BLOB NOT NULL, added_ts INTEGER);
CREATE TABLE skipped(file_unique_id TEXT PRIMARY KEY, reason TEXT, ts INTEGER);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def make_db(path: Path, rows: dict[int, str]) -> None:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute(fts_create_sql())
    db.execute("INSERT INTO meta VALUES('dim','4')")
    for sid, desc in rows.items():
        add_row(db, sid, desc)
    db.commit()
    db.close()


def add_row(db, sid, desc):
    db.execute("INSERT INTO stickers(id,file_unique_id,file_id,set_name,set_title,emoji,kind,"
               "description,characters,objects,actions,emotions,visual_tags,style_tags,emb) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (sid, f"u{sid}", f"f{sid}", "pack", "Pack", "x", "regular", desc,
                "[]", "[]", "[]", "[]", "[]", "[]", bytes([sid % 256]) * 16))
    db.execute(f"INSERT INTO fts_stickers(rowid,{','.join(FTS_COLUMNS)}) VALUES(?{',?' * len(FTS_COLUMNS)})",
               (sid, *fts_values(desc, {}, "x", "Pack")))


def edit(path: Path, sid: int, desc: str) -> None:
    db = sqlite3.connect(path)
    db.execute("UPDATE stickers SET description=?, emb=? WHERE id=?", (desc, b"E" * 16, sid))
    db.execute("UPDATE fts_stickers SET description=? WHERE rowid=?", (desc, sid))
    db.commit()
    db.close()


def desc(path: Path, sid: int):
    db = sqlite3.connect(path)
    r = db.execute("SELECT description FROM stickers WHERE id=?", (sid,)).fetchone()
    db.close()
    return r[0] if r else None


def fts_hits(path: Path, word: str) -> list[int]:
    db = sqlite3.connect(path)
    r = [x[0] for x in db.execute(
        "SELECT rowid FROM fts_stickers WHERE fts_stickers MATCH ? ORDER BY rowid", (word,))]
    db.close()
    return r


class Side:

    def __init__(self, root: Path, side: str, drive: Path, marks: Path):
        self.cfg = dbsync.Config(side=side, remote=f"dir:{drive}", data_dir=root / side)
        self.cfg.data_dir.mkdir(parents=True)
        self.marks = marks
        if side == "vds":
            self.cfg.stop_cmd = f"echo stop >> {marks}"
            self.cfg.start_cmd = f"echo start >> {marks}"

    def db(self, name="stickers") -> Path:
        return self.cfg.db_path(name)

    def sy(self):
        return dbsync.Syncer(self.cfg)

    def sync(self, dry=False):
        sy = self.sy()
        try:
            return sy.sync_stickers(dry)
        finally:
            sy.close()

    def restarts(self):
        return self.marks.read_text().split() if self.marks.exists() else []


class Sync(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        drive = self.root / "drive"
        self.laptop = Side(self.root, "laptop", drive, self.root / "laptop.marks")
        self.vds = Side(self.root, "vds", drive, self.root / "vds.marks")

    def tearDown(self):
        self._t.cleanup()

    def bootstrap(self):
        make_db(self.laptop.db(), {1: "a red fox", 2: "a blue wolf", 3: "green dragon"})
        self.assertIn("first version", self.laptop.sync())
        import shutil
        shutil.copy(self.laptop.db(), self.vds.db())
        out = self.vds.sync()
        self.assertTrue(out.startswith("stickers:"), out)

    def test_new_rows_flow_to_vds_and_restart_once(self):
        self.bootstrap()
        marks_before = self.vds.restarts()
        db = sqlite3.connect(self.laptop.db())
        add_row(db, 4, "purple cat sleeping")
        db.commit()
        db.close()
        self.assertIn("uploaded", self.laptop.sync())
        out = self.vds.sync()
        self.assertIn("+1 new", out)
        self.assertEqual(desc(self.vds.db(), 4), "purple cat sleeping")
        self.assertEqual(fts_hits(self.vds.db(), "purple"), [4])        # FTS row came along
        self.assertEqual(self.vds.restarts()[len(marks_before):], ["stop", "start"])
        n = len(self.vds.restarts())
        self.assertIn("up to date", self.vds.sync())
        self.assertEqual(len(self.vds.restarts()), n)

    def test_vds_edit_and_laptop_rows_are_both_kept(self):
        self.bootstrap()
        edit(self.vds.db(), 2, "a very blue wolf howling")
        self.assertIn("uploaded", self.vds.sync())            # the edit goes up
        db = sqlite3.connect(self.laptop.db())
        add_row(db, 4, "yellow duck")
        db.commit()
        db.close()
        out = self.laptop.sync()                              # laptop: merge + push
        self.assertIn("uploaded", out)
        self.assertEqual(desc(self.laptop.db(), 2), "a very blue wolf howling")
        self.assertEqual(desc(self.laptop.db(), 4), "yellow duck")
        self.assertEqual(fts_hits(self.laptop.db(), "howling"), [2])
        self.vds.sync()
        self.assertEqual(desc(self.vds.db(), 4), "yellow duck")
        self.assertEqual(desc(self.vds.db(), 2), "a very blue wolf howling")

    def test_conflict_vds_wins_everywhere(self):
        self.bootstrap()
        edit(self.vds.db(), 1, "vds says fox")
        edit(self.laptop.db(), 1, "laptop says fox")
        self.vds.sync()                                        # vds uploads first
        out = self.laptop.sync()
        self.assertIn("1 conflicts", out)
        self.assertEqual(desc(self.laptop.db(), 1), "vds says fox")
        edit(self.laptop.db(), 2, "laptop wolf")
        edit(self.vds.db(), 2, "vds wolf")
        self.laptop.sync()
        self.vds.sync()
        self.assertEqual(desc(self.vds.db(), 2), "vds wolf")
        self.laptop.sync()
        self.assertEqual(desc(self.laptop.db(), 2), "vds wolf")

    def test_diverged_ids_abort_and_leave_the_live_db_alone(self):
        self.bootstrap()
        for side, fuid in ((self.laptop, "uA"), (self.vds, "uB")):
            db = sqlite3.connect(side.db())
            add_row(db, 9, "x")
            db.execute("UPDATE stickers SET file_unique_id=? WHERE id=9", (fuid,))
            db.commit()
            db.close()
        self.vds.sync()
        before = desc(self.laptop.db(), 9)
        with self.assertRaises(dbsync.SyncError):
            self.laptop.sync()
        self.assertEqual(desc(self.laptop.db(), 9), before)

    def test_dry_run_changes_nothing(self):
        self.bootstrap()
        db = sqlite3.connect(self.laptop.db())
        add_row(db, 4, "purple cat")
        db.commit()
        db.close()
        self.laptop.sync()
        before = self.vds.db().stat().st_mtime_ns
        out = self.vds.sync(dry=True)
        self.assertIn("dry run", out)
        self.assertEqual(self.vds.db().stat().st_mtime_ns, before)
        self.assertIsNone(desc(self.vds.db(), 4))

    def test_swap_keeps_a_backup_and_drops_stale_wal(self):
        self.bootstrap()
        (Path(str(self.vds.db()) + "-wal")).write_bytes(b"stale")
        db = sqlite3.connect(self.laptop.db())
        add_row(db, 4, "purple cat")
        db.commit()
        db.close()
        self.laptop.sync()
        self.vds.sync()
        self.assertFalse(Path(str(self.vds.db()) + "-wal").exists())
        self.assertTrue(list(self.vds.cfg.data_dir.glob("stickers.db.bak-*")))

    def test_start_runs_even_if_the_swap_fails(self):
        self.bootstrap()
        db = sqlite3.connect(self.laptop.db())
        add_row(db, 4, "purple cat")
        db.commit()
        db.close()
        self.laptop.sync()
        lock = sqlite3.connect(self.vds.db(), isolation_level=None)
        lock.execute("BEGIN")
        lock.execute("SELECT * FROM stickers").fetchone()
        try:
            with self.assertRaises(dbsync.SyncError):
                self.vds.sync()
        finally:
            lock.execute('ROLLBACK')
            lock.close()
        self.assertEqual(self.vds.restarts()[-2:], ["stop", "start"])

    def test_fresh_machine_downloads_the_master_copy(self):
        make_db(self.laptop.db(), {1: "a red fox", 2: "a blue wolf"})
        self.laptop.sync()
        self.assertIn("fresh machine", self.vds.sync())
        self.assertEqual(desc(self.vds.db(), 2), "a blue wolf")
        self.assertIn("up to date", self.vds.sync())
        self.assertEqual(self.vds.restarts(), [])              # nothing was running

    def test_corrupt_remote_is_refused(self):
        self.bootstrap()
        blob = Path(self.laptop.cfg.remote[4:]) / "stickers.db.gz"
        blob.write_bytes(b"not a gzip at all")
        with self.assertRaises(dbsync.SyncError):
            self.vds.sync()


class OneWay(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        root = Path(self._t.name)
        drive = root / "drive"
        self.laptop = Side(root, "laptop", drive, root / "l")
        self.vds = Side(root, "vds", drive, root / "v")

    def tearDown(self):
        self._t.cleanup()

    def test_bot_data_goes_vds_to_laptop_only(self):
        db = sqlite3.connect(self.vds.db("bot_data"))
        db.execute("CREATE TABLE pack_requests(pack TEXT, status TEXT)")
        db.execute("INSERT INTO pack_requests VALUES('NewPack','approved')")
        db.commit()
        db.close()
        sy = self.laptop.sy()
        self.addCleanup(sy.close)
        with self.assertRaises(dbsync.SyncError):                 # wrong side pushes
            sy.push("bot_data")
        sv = self.vds.sy()
        self.addCleanup(sv.close)
        with self.assertRaises(dbsync.SyncError):                 # nothing to pull yet
            sy.pull("bot_data")
        self.assertIn("pushed", sv.push("bot_data"))
        with self.assertRaises(dbsync.SyncError):                 # vds must not pull over its data
            sv.pull("bot_data")
        self.assertIn("unchanged", sv.push("bot_data", if_changed=True))   # no re-upload
        self.assertIn("pulled", sy.pull("bot_data"))
        got = sqlite3.connect(self.laptop.db("bot_data")).execute(
            "SELECT pack, status FROM pack_requests").fetchall()
        self.assertEqual(got, [("NewPack", "approved")])

    def test_status_and_config(self):
        cfg = dbsync.load_config(str(self.laptop.cfg.data_dir / "nope.toml"))
        self.assertEqual(cfg.side, "laptop")
        os.environ["DBSYNC_SIDE"] = "vds"
        try:
            self.assertEqual(dbsync.load_config(None).side, "vds")
        finally:
            del os.environ["DBSYNC_SIDE"]
        with self.assertRaises(dbsync.SyncError):
            os.environ["DBSYNC_SIDE"] = "moon"
            try:
                dbsync.load_config(None)
            finally:
                del os.environ["DBSYNC_SIDE"]


class FakeReq:
    def __init__(self, result=None, exc=None):
        self.result, self.exc = result, exc

    def execute(self, **kw):
        if self.exc:
            raise self.exc
        return self.result

    def next_chunk(self, **kw):
        return None, self.result


class FakeFiles:
    def __init__(self, existing, quota=False):
        self.existing, self.quota, self.created, self.queries, self.updated = existing, quota, [], [], []

    def list(self, q, **kw):
        self.queries.append(q)
        name = q.split("'")[1]
        return FakeReq({"files": [f for f in self.existing if f["name"] == name]})

    def create(self, body, **kw):
        from googleapiclient.errors import HttpError

        class R:
            status = 403
            reason = "forbidden"
        if self.quota:
            return FakeReq(exc=HttpError(R(), b"storageQuotaExceeded"))
        self.created.append(body["name"])
        return FakeReq({"id": "new"})

    def update(self, fileId, media_body, **kw):
        self.updated.append(fileId)
        import hashlib
        data = Path(media_body._filename).read_bytes()
        return FakeReq({"id": fileId, "md5Checksum": hashlib.md5(data).hexdigest(),
                        "size": str(len(data)), "modifiedTime": "t"})


try:
    import googleapiclient.errors  # noqa: F401
    HAVE_GOOGLE = True
except ImportError:
    HAVE_GOOGLE = False


@unittest.skipUnless(HAVE_GOOGLE, "google-api-python-client not installed")
class DriveRemoteLogic(unittest.TestCase):

    def make(self, existing, quota=False):
        r = dbsync.DriveRemote.__new__(dbsync.DriveRemote)
        r.folder, r._ids = "FOLDER", {}
        files = FakeFiles(existing, quota)
        r.svc = type("S", (), {"files": lambda self: files})()
        return r, files

    def test_placeholder_is_nothing_and_real_file_is_found(self):
        r, files = self.make([{"id": "1", "name": "a.db.gz", "size": "0"},
                              {"id": "2", "name": "b.db.gz", "size": "10", "md5Checksum": "m"}])
        self.assertIsNone(r.stat("a.db.gz"))
        self.assertEqual(r.stat("b.db.gz").md5, "m")
        self.assertIn("'FOLDER' in parents", files.queries[0])

    def test_init_reports_what_to_create_by_hand_on_quota_error(self):
        r, _ = self.make([], quota=True)
        self.assertEqual(r.init(["x.db.gz"]), ["x.db.gz"])
        r, files = self.make([])
        self.assertEqual(r.init(["x.db.gz"]), [])
        self.assertEqual(files.created, ["x.db.gz"])

    def test_upload_requires_the_file_and_checks_md5(self):
        r, files = self.make([{"id": "7", "name": "s.db.gz", "size": "0"}])
        src = Path(tempfile.mkdtemp()) / "s.db.gz"
        src.write_bytes(b"data")
        self.assertEqual(r.upload("s.db.gz", src).size, 4)
        self.assertEqual(files.updated, ["7"])
        r2, _ = self.make([])
        with self.assertRaises(dbsync.SyncError):
            r2.upload("s.db.gz", src)


if __name__ == "__main__":
    unittest.main()


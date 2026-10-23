# /// script
# requires-python = ">=3.11"
# dependencies = ["google-api-python-client>=2.100", "google-auth>=2.23"]
# ///
"""Sync the stickersbot SQLite databases between the laptop, Google Drive and the VDS."""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("dbsync")
ROOT = Path(__file__).resolve().parent.parent
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
CHUNK = 16 * 1024 * 1024


class SyncError(RuntimeError):
    pass


# ------------------------------------------------------------------ what is synced
@dataclass(frozen=True)
class Spec:
    mode: str            # "merge" (two-way) or "owned" (one writer, the others read)
    owner: str = ""      # for "owned": the side that writes it ("laptop" | "vds")


DBS = {
    "stickers": Spec("merge"),
    "bot_data": Spec("owned", "vds"),
    "raw_captions": Spec("owned", "laptop"),
}


def blob_name(name: str) -> str:
    return f"{name}.db.gz"


# ------------------------------------------------------------------ config
@dataclass
class Config:
    side: str = "laptop"                 # "laptop" | "vds"
    remote: str = "drive"                # "drive" or "dir:/some/folder" (tests, shared mounts)
    folder_id: str = ""
    sa_file: str = ""
    sa_json: str = ""
    data_dir: Path = ROOT / "data"
    stop_cmd: str = ""                   # run before the live stickers.db is replaced
    start_cmd: str = ""                  # run afterwards (also after a failure)
    keep_backups: int = 2
    extra: dict = field(default_factory=dict)

    @property
    def work_dir(self) -> Path:
        return self.data_dir / ".sync"

    def db_path(self, name: str) -> Path:
        return self.data_dir / f"{name}.db"


def load_config(path: str | None = None) -> Config:
    cfg = Config()
    f = Path(path) if path else ROOT / "dbsync.toml"
    if f.exists():
        raw = tomllib.loads(f.read_text(encoding="utf-8"))
        for k, v in raw.items():
            if k == "data_dir":
                v = (f.parent / v).resolve()
            if hasattr(cfg, k):
                setattr(cfg, k, v)
            else:
                cfg.extra[k] = v
    env = os.environ
    for key, attr in (("SIDE", "side"), ("REMOTE", "remote"), ("FOLDER_ID", "folder_id"),
                      ("SA_FILE", "sa_file"), ("SA_JSON", "sa_json"),
                      ("STOP_CMD", "stop_cmd"), ("START_CMD", "start_cmd")):
        if env.get(f"DBSYNC_{key}"):
            setattr(cfg, attr, env[f"DBSYNC_{key}"])
    if env.get("DBSYNC_DATA_DIR"):
        cfg.data_dir = Path(env["DBSYNC_DATA_DIR"]).resolve()
    cfg.data_dir = Path(cfg.data_dir)
    if cfg.side not in ("laptop", "vds"):
        raise SyncError(f"side must be 'laptop' or 'vds', not {cfg.side!r}")
    return cfg


# ------------------------------------------------------------------ remotes
@dataclass
class RemoteInfo:
    md5: str
    size: int
    modified: str = ""


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DirRemote:

    def __init__(self, folder: str):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)

    def init(self, names: list[str]) -> list[str]:
        return []

    def stat(self, name: str) -> RemoteInfo | None:
        p = self.dir / name
        if not p.exists() or p.stat().st_size == 0:
            return None
        return RemoteInfo(md5_of(p), p.stat().st_size,
                          time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(p.stat().st_mtime)))

    def download(self, name: str, dest: Path) -> RemoteInfo:
        info = self.stat(name)
        if info is None:
            raise SyncError(f"{name}: nothing on the remote yet")
        shutil.copyfile(self.dir / name, dest)
        return info

    def upload(self, name: str, src: Path) -> RemoteInfo:
        tmp = self.dir / (name + ".part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, self.dir / name)
        return self.stat(name)                       # type: ignore[return-value]


class DriveRemote:

    def __init__(self, folder_id: str, sa_file: str = "", sa_json: str = ""):
        if not folder_id:
            raise SyncError("folder_id is not set (the id of the Drive folder shared with the "
                            "service account): dbsync.toml or DBSYNC_FOLDER_ID")
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as e:
            raise SyncError("the Google libraries are missing: run the tool with "
                            "`uv run tools/dbsync.py ...` or `pip install google-api-python-client "
                            "google-auth`") from e
        if sa_json.strip():
            creds = service_account.Credentials.from_service_account_info(
                json.loads(sa_json), scopes=[DRIVE_SCOPE])
        elif sa_file:
            creds = service_account.Credentials.from_service_account_file(
                sa_file, scopes=[DRIVE_SCOPE])
        else:
            raise SyncError("no service account: sa_file in dbsync.toml, or DBSYNC_SA_FILE / "
                            "DBSYNC_SA_JSON")
        self.folder = folder_id
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._ids: dict[str, str] = {}

    def _find(self, name: str) -> dict | None:
        q = f"name = '{name}' and '{self.folder}' in parents and trashed = false"
        res = self.svc.files().list(
            q=q, spaces="drive", pageSize=10, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            fields="files(id,name,md5Checksum,size,modifiedTime)").execute(num_retries=5)
        files = res.get("files", [])
        if len(files) > 1:
            raise SyncError(f"{len(files)} files named {name} in the folder - delete the extras")
        if files:
            self._ids[name] = files[0]["id"]
        return files[0] if files else None

    @staticmethod
    def _info(f: dict) -> RemoteInfo | None:
        size = int(f.get("size") or 0)
        if size == 0:
            return None                              # an empty placeholder = nothing yet
        return RemoteInfo(f.get("md5Checksum", ""), size, f.get("modifiedTime", ""))

    def init(self, names: list[str]) -> list[str]:
        from googleapiclient.errors import HttpError
        manual = []
        for n in names:
            if self._find(n):
                continue
            try:
                self.svc.files().create(
                    body={"name": n, "parents": [self.folder]}, supportsAllDrives=True,
                    fields="id").execute(num_retries=3)
                log.info("created %s", n)
            except HttpError as e:
                if e.resp.status == 403:
                    manual.append(n)
                else:
                    raise
        return manual

    def stat(self, name: str) -> RemoteInfo | None:
        f = self._find(name)
        return self._info(f) if f else None

    def download(self, name: str, dest: Path) -> RemoteInfo:
        from googleapiclient.http import MediaIoBaseDownload
        f = self._find(name)
        info = self._info(f) if f else None
        if info is None:
            raise SyncError(f"{name}: nothing on the remote yet")
        req = self.svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=CHUNK)
            done = False
            while not done:
                status, done = dl.next_chunk(num_retries=5)
                if status:
                    log.info("  download %s: %d%%", name, int(status.progress() * 100))
        if info.md5 and md5_of(dest) != info.md5:
            raise SyncError(f"{name}: the download is corrupted (md5 mismatch), try again")
        return info

    def upload(self, name: str, src: Path) -> RemoteInfo:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        f = self._find(name)
        if f is None:
            raise SyncError(f"{name} does not exist in the Drive folder yet: run `init` "
                            f"(it explains how to create the placeholder files)")
        media = MediaFileUpload(str(src), mimetype="application/gzip", resumable=True,
                                chunksize=CHUNK)
        req = self.svc.files().update(fileId=f["id"], media_body=media, supportsAllDrives=True,
                                      fields="id,md5Checksum,size,modifiedTime")
        try:
            resp = None
            while resp is None:
                status, resp = req.next_chunk(num_retries=5)
                if status:
                    log.info("  upload %s: %d%%", name, int(status.progress() * 100))
        except HttpError as e:
            raise SyncError(f"Drive refused the upload of {name}: {e}") from e
        info = self._info(resp)
        if info is None or (info.md5 and info.md5 != md5_of(src)):
            raise SyncError(f"{name}: the upload did not arrive intact (md5 mismatch)")
        return info


def make_remote(cfg: Config):
    if cfg.remote.startswith("dir:"):
        return DirRemote(cfg.remote[4:])
    if cfg.remote != "drive":
        raise SyncError(f"unknown remote {cfg.remote!r} (use 'drive' or 'dir:<folder>')")
    return DriveRemote(cfg.folder_id, cfg.sa_file, cfg.sa_json)


# ------------------------------------------------------------------ sqlite helpers
def snapshot(src: Path, dst: Path) -> None:
    if not src.exists():
        raise SyncError(f"{src}: no such database")
    dst.unlink(missing_ok=True)
    s = sqlite3.connect(str(src), timeout=60)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def check_db(path: Path, need_table: str | None = None) -> None:
    db = sqlite3.connect(str(path))
    try:
        res = db.execute("PRAGMA quick_check").fetchone()
        if not res or res[0] != "ok":
            raise SyncError(f"{path.name}: sqlite integrity check failed: {res}")
        if need_table and not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (need_table,)).fetchone():
            raise SyncError(f"{path.name}: no table '{need_table}' - not the right database")
    finally:
        db.close()


def gz_file(src: Path, dst: Path) -> None:
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        with gzip.GzipFile(fileobj=fo, mode="wb", compresslevel=4, mtime=0) as gz:
            shutil.copyfileobj(fi, gz, 1 << 20)


def gunzip_file(src: Path, dst: Path) -> None:
    try:
        with gzip.open(src, "rb") as fi, open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
    except (OSError, EOFError) as e:
        raise SyncError(f"{src.name}: not a valid gzip file ({e})") from e


def swap_in(live: Path, new: Path, keep: int = 2) -> None:
    if live.exists():
        db = sqlite3.connect(str(live), timeout=3)
        try:
            db.execute("BEGIN EXCLUSIVE")
            db.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            raise SyncError(f"{live.name} is in use (bot or parser still running?): {e}") from e
        finally:
            db.close()
        bak = live.with_name(f"{live.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copyfile(live, bak)
        old = sorted(live.parent.glob(f"{live.name}.bak-*"))
        for p in old[:-keep] if keep > 0 else old:
            p.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(live) + suffix).unlink(missing_ok=True)   # stale side files of the OLD file
    os.replace(new, live)


# ------------------------------------------------------------------ stickers: 3-way merge
def _columns(db: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA {schema}.table_info({table})")]


def row_hashes(path: Path) -> dict[int, tuple[str, bytes]]:
    db = sqlite3.connect(str(path))
    try:
        cols = [c for c in _columns(db, "stickers") if c != "id"]
        out = {}
        fu = cols.index("file_unique_id")
        for row in db.execute(f"SELECT id, {', '.join(cols)} FROM stickers"):
            h = hashlib.sha1()
            for v in row[1:]:
                h.update(v if isinstance(v, bytes) else repr(v).encode())
                h.update(b"\x1f")
            out[row[0]] = (row[1 + fu], h.digest())
        return out
    finally:
        db.close()


def load_base(path: Path) -> dict[int, tuple[str, bytes]] | None:
    if not path.exists():
        return None
    db = sqlite3.connect(str(path))
    try:
        return {i: (f, bytes(h)) for i, f, h in db.execute("SELECT id, fuid, h FROM base")}
    except sqlite3.Error:
        return None
    finally:
        db.close()


def save_base(path: Path, hashes: dict[int, tuple[str, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(str(tmp))
    db.execute("CREATE TABLE base(id INTEGER PRIMARY KEY, fuid TEXT, h BLOB)")
    db.executemany("INSERT INTO base VALUES(?,?,?)", ((i, f, h) for i, (f, h) in hashes.items()))
    db.commit()
    db.close()
    os.replace(tmp, path)


@dataclass
class MergeResult:
    added: int = 0            # rows that only the remote had (-> local)
    updated: int = 0          # rows changed on the remote only (-> local)
    conflicts: int = 0        # changed on both sides; the winner is chosen by `prefer`
    taken_remote: int = 0     # of those conflicts: how many went to the remote's version
    skipped_added: int = 0
    push_needed: bool = False  # the merged result differs from the remote

    @property
    def applied(self) -> int:
        return self.added + self.updated + self.taken_remote + self.skipped_added


def merge_stickers(local: Path, remote: Path, base: dict | None, prefer: str) -> MergeResult:
    res = MergeResult()
    base = base or {}
    hr = row_hashes(remote)
    hl = row_hashes(local)
    db = sqlite3.connect(str(local), isolation_level=None)
    try:
        db.execute("ATTACH DATABASE ? AS r", (str(remote),))
        cols_l, cols_r = _columns(db, "stickers"), _columns(db, "stickers", "r")
        if cols_l != cols_r:
            raise SyncError("the stickers tables of the two databases have different columns "
                            "(one side is on another schema version): migrate it first")
        dims = [db.execute(f"SELECT value FROM {s}.meta WHERE key='dim'").fetchone()
                for s in ("main", "r")]
        if all(dims) and dims[0][0] != dims[1][0]:
            raise SyncError(f"embedding dimension differs (local {dims[0][0]}, remote "
                            f"{dims[1][0]}): the two databases use different models")
        fuid_to_id = {f: i for i, (f, _) in hl.items()}
        fts_cols = [c for c in _columns(db, "fts_stickers") if c in _columns(db, "fts_stickers", "r")]
        fts_list = ", ".join(fts_cols)

        todo: list[int] = []
        for sid, (fuid, rh) in hr.items():
            if sid not in hl:
                if fuid in fuid_to_id:
                    raise SyncError(
                        f"sticker {fuid} has id {fuid_to_id[fuid]} here but id {sid} on the "
                        f"remote: the databases diverged (both sides inserted rows). "
                        f"Keep one as the master and re-create the other from it")
                res.added += 1
                todo.append(sid)
                continue
            lf, lh = hl[sid]
            if lf != fuid:
                raise SyncError(f"id {sid} is sticker {lf} here but {fuid} on the remote: "
                                f"the databases diverged")
            if lh == rh:
                continue
            bh = base.get(sid, (None, None))[1]
            if bh is None or (lh != bh and rh != bh):
                res.conflicts += 1                 # changed on both sides (or no common base)
                if prefer == "remote":
                    res.taken_remote += 1
                    todo.append(sid)
            elif lh == bh:                         # only the remote changed it
                res.updated += 1
                todo.append(sid)

        db.execute("BEGIN IMMEDIATE")
        try:
            for sid in todo:
                db.execute("INSERT OR REPLACE INTO main.stickers SELECT * FROM r.stickers "
                           "WHERE id=?", (sid,))
                db.execute("DELETE FROM main.fts_stickers WHERE rowid=?", (sid,))
                db.execute(f"INSERT INTO main.fts_stickers(rowid, {fts_list}) "
                           f"SELECT rowid, {fts_list} FROM r.fts_stickers WHERE rowid=?", (sid,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='skipped'").fetchone():
                res.skipped_added = db.execute(
                    "SELECT COUNT(*) FROM r.skipped WHERE file_unique_id NOT IN "
                    "(SELECT file_unique_id FROM main.skipped)").fetchone()[0]
                db.execute("INSERT OR IGNORE INTO main.skipped SELECT * FROM r.skipped")
                res.push_needed |= bool(db.execute(
                    "SELECT 1 FROM main.skipped WHERE file_unique_id NOT IN "
                    "(SELECT file_unique_id FROM r.skipped) LIMIT 1").fetchone())
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("DETACH DATABASE r")
    finally:
        db.close()
    res.push_needed |= row_hashes(local) != hr
    return res


# ------------------------------------------------------------------ state
class State:
    def __init__(self, cfg: Config):
        self.path = cfg.work_dir / "state.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, name: str) -> dict:
        return self.data.get(name, {})

    def set(self, name: str, **kw) -> None:
        self.data.setdefault(name, {}).update(kw, synced_at=int(time.time()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


def run_cmd(cmd: str, what: str) -> None:
    if not cmd:
        return
    log.info("%s: %s", what, cmd)
    r = subprocess.run(cmd, shell=True)
    if r.returncode:
        raise SyncError(f"{what} failed (exit {r.returncode}): {cmd}")


# ------------------------------------------------------------------ operations
class Syncer:
    def __init__(self, cfg: Config, remote=None):
        self.cfg = cfg
        self.remote = remote or make_remote(cfg)
        self.state = State(cfg)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="dbsync-", dir=cfg.work_dir))

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload(self, name: str, db_file: Path, expect_md5: str | None) -> RemoteInfo:
        gz = self.tmp / blob_name(name)
        gz_file(db_file, gz)
        now = self.remote.stat(blob_name(name))
        if expect_md5 is not None and (now.md5 if now else None) != (expect_md5 or None):
            raise SyncError(f"{name}: the remote changed while we were working - "
                            f"run the sync again")
        log.info("uploading %s (%.1f MB)", blob_name(name), gz.stat().st_size / 1e6)
        info = self.remote.upload(blob_name(name), gz)
        gz.unlink(missing_ok=True)
        return info

    def _download(self, name: str) -> tuple[Path, RemoteInfo]:
        gz = self.tmp / ("dl-" + blob_name(name))
        info = self.remote.download(blob_name(name), gz)
        out = self.tmp / f"remote-{name}.db"
        gunzip_file(gz, out)
        gz.unlink(missing_ok=True)
        check_db(out, "stickers" if name == "stickers" else None)
        return out, info

    def sync_stickers(self, dry_run: bool = False) -> str:
        name, cfg = "stickers", self.cfg
        live = cfg.db_path(name)
        base_file = cfg.work_dir / "stickers.base.sqlite"
        prefer = "local" if cfg.side == "vds" else "remote"     # the VDS wins conflicts
        info = self.remote.stat(blob_name(name))
        st = self.state.get(name)
        base = load_base(base_file)
        snap = self.tmp / "local-snapshot.db"
        if not live.exists():                                    # a fresh machine
            if info is None:
                raise SyncError("stickers: neither a local nor a remote database exists yet")
            log.info("stickers: no local database - taking the remote one")
            if dry_run:
                return "stickers (dry run): would download the remote database"
            remote_db, rinfo = self._download(name)
            live.parent.mkdir(parents=True, exist_ok=True)
            staged = live.with_name(live.name + ".incoming")
            shutil.move(str(remote_db), staged)
            swap_in(live, staged, cfg.keep_backups)
            save_base(base_file, row_hashes(live))
            self.state.set(name, remote_md5=rinfo.md5)
            return "stickers: downloaded (fresh machine)"
        snapshot(live, snap)
        check_db(snap, "stickers")

        if info is None:                                         # first ever upload
            msg = f"stickers: the remote is empty - uploading {live.name} as the first version"
            log.info(msg)
            if dry_run:
                return msg
            up = self._upload(name, snap, "")
            save_base(base_file, row_hashes(snap))
            self.state.set(name, remote_md5=up.md5)
            return "stickers: uploaded (first version)"

        remote_changed = base is None or info.md5 != st.get("remote_md5")
        if not remote_changed:
            hl = row_hashes(snap)
            if hl == base:
                return "stickers: up to date"
            msg = "stickers: local changes -> uploading"
            log.info(msg)
            if dry_run:
                return msg + " (dry run)"
            up = self._upload(name, snap, info.md5)
            save_base(base_file, hl)
            self.state.set(name, remote_md5=up.md5)
            return "stickers: uploaded local changes"

        remote_db, rinfo = self._download(name)
        res = merge_stickers(snap, remote_db, base, prefer)
        log.info("stickers merge: +%d rows from the remote, %d updated, %d conflicts "
                 "(%d went to the remote), push needed: %s", res.added, res.updated,
                 res.conflicts, res.taken_remote, res.push_needed)
        if dry_run:
            return (f"stickers (dry run): +{res.added} new, {res.updated} updated, "
                    f"{res.conflicts} conflicts, push needed: {res.push_needed}")

        final = snap
        if res.applied:
            run_cmd(cfg.stop_cmd, "stop")
            try:
                snapshot(live, snap)
                res = merge_stickers(snap, remote_db, base, prefer)
                check_db(snap, "stickers")
                swap_in(live, snap, cfg.keep_backups)
            finally:
                run_cmd(cfg.start_cmd, "start")
            final = self.tmp / "final.db"            # what we upload is a snapshot of the
            snapshot(live, final)                    # LIVE file, never the file being written
        merged_hashes = row_hashes(final)

        if res.push_needed:
            up = self._upload(name, final, rinfo.md5)
            self.state.set(name, remote_md5=up.md5)
            tail = "merged and uploaded"
        else:
            self.state.set(name, remote_md5=rinfo.md5)
            tail = "merged" if res.applied else "nothing to merge"
        save_base(base_file, merged_hashes)
        return (f"stickers: {tail} (+{res.added} new, {res.updated} updated, "
                f"{res.conflicts} conflicts)" + (" - bot restarted" if res.applied and
                                                  cfg.start_cmd else ""))

    def push(self, name: str, force: bool = False, if_changed: bool = False) -> str:
        spec = DBS[name]
        if spec.mode == "owned" and spec.owner != self.cfg.side and not force:
            raise SyncError(f"{name} is written on the {spec.owner}; pushing it from the "
                            f"{self.cfg.side} would overwrite the real data (use --force)")
        snap = self.tmp / f"{name}.snap.db"
        snapshot(self.cfg.db_path(name), snap)
        check_db(snap)
        sha = hashlib.sha256(snap.read_bytes()).hexdigest()
        if if_changed and sha == self.state.get(name).get("pushed_sha"):
            return f"{name}: unchanged since the last push"
        up = self._upload(name, snap, None)
        self.state.set(name, remote_md5=up.md5, pushed_sha=sha)
        return f"{name}: pushed ({up.size / 1e6:.1f} MB)"

    def pull(self, name: str, to: Path | None = None, force: bool = False) -> str:
        spec = DBS[name]
        if spec.mode == "owned" and spec.owner == self.cfg.side and not force:
            raise SyncError(f"{name} is written on this side ({self.cfg.side}); pulling would "
                            f"replace the live data with an older copy (use --force)")
        if spec.mode == "merge":
            raise SyncError(f"{name} is merged, use: sync {name}")
        db_file, info = self._download(name)
        dest = to or self.cfg.db_path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staged = dest.with_name(dest.name + ".incoming")
        shutil.move(str(db_file), staged)
        swap_in(dest, staged, self.cfg.keep_backups)
        self.state.set(name, remote_md5=info.md5)
        return f"{name}: pulled to {dest}"


# ------------------------------------------------------------------ commands
def cmd_status(cfg: Config, sy: Syncer) -> int:
    print(f"side: {cfg.side}   remote: {cfg.remote}   data: {cfg.data_dir}")
    for name, spec in DBS.items():
        p = cfg.db_path(name)
        local = "absent"
        if p.exists():
            local = f"{p.stat().st_size / 1e6:.1f} MB"
            with contextlib.suppress(sqlite3.Error):
                db = sqlite3.connect(str(p))
                tbl = "stickers" if name == "stickers" else None
                if tbl:
                    local += f", {db.execute('SELECT COUNT(*) FROM stickers').fetchone()[0]} stickers"
                db.close()
        info = sy.remote.stat(blob_name(name))
        remote = f"{info.size / 1e6:.1f} MB, {info.modified}" if info else "nothing yet"
        who = "two-way merge" if spec.mode == "merge" else f"written on the {spec.owner}"
        print(f"  {name:13} {who:22} local: {local:28} remote: {remote}")
    return 0


def cmd_init(cfg: Config, sy: Syncer) -> int:
    manual = sy.remote.init([blob_name(n) for n in DBS])
    if not manual:
        print("OK: the folder is reachable and every file exists")
        return 0
    print("The service account cannot create files in a normal 'My Drive' folder (Google gives\n"
          "service accounts no storage quota). Create these EMPTY files in the shared folder\n"
          "yourself (e.g. New -> File upload of an empty text file, then rename), exactly named:\n")
    for n in manual:
        print(f"    {n}")
    print("\nThey stay yours; the service account only updates their content. Then run `init` "
          "again.\n(Or put the folder on a Shared Drive: no placeholders are needed there.)")
    return 1


def cmd_all(cfg: Config, sy: Syncer, dry_run: bool) -> int:
    if cfg.side == "vds":
        steps = [("stickers", lambda: sy.sync_stickers(dry_run)),
                 ("bot_data", lambda: sy.push("bot_data", if_changed=True))]
    else:
        steps = [("bot_data", lambda: sy.pull("bot_data")),
                 ("stickers", lambda: sy.sync_stickers(dry_run)),
                 ("raw_captions", lambda: sy.push("raw_captions", if_changed=True))]
    needs_local = {"raw_captions"} | ({"bot_data"} if cfg.side == "vds" else set())
    rc = 0
    for name, fn in steps:
        if name in needs_local and not cfg.db_path(name).exists():
            log.info("%s: no local file, skipped", name)
            continue
        try:
            print(fn())
        except SyncError as e:
            log.error("%s: %s", name, e)
            rc = 1
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to dbsync.toml (default: <repo>/dbsync.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="check access, create/explain the Drive files")
    sub.add_parser("status", help="local vs remote overview")
    p = sub.add_parser("all", help="everything this side should do (see the module docstring)")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("sync", help="two-way merge (stickers)")
    p.add_argument("name", choices=[n for n, s in DBS.items() if s.mode == "merge"])
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("push", help="upload a snapshot of a database")
    p.add_argument("name", choices=list(DBS))
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("pull", help="download a database (the old file is kept as .bak-*)")
    p.add_argument("name", choices=[n for n, s in DBS.items() if s.mode == "owned"])
    p.add_argument("--to", help="destination file (default: <data>/<name>.db)")
    p.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
        sy = Syncer(cfg)
        try:
            if args.cmd == "init":
                return cmd_init(cfg, sy)
            if args.cmd == "status":
                return cmd_status(cfg, sy)
            if args.cmd == "all":
                return cmd_all(cfg, sy, args.dry_run)
            if args.cmd == "sync":
                print(sy.sync_stickers(args.dry_run))
            elif args.cmd == "push":
                print(sy.push(args.name, args.force))
            elif args.cmd == "pull":
                print(sy.pull(args.name, Path(args.to) if args.to else None, args.force))
            return 0
        finally:
            sy.close()
    except SyncError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

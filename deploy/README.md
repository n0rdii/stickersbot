# Deploy & database sync

```
laptop (parser, JoyCaption) <--dbsync--> Google Drive folder <--dbsync--> VDS (bot)
```

## 1. Google Drive (once)
1. Google Cloud console -> create a project -> enable **Google Drive API**.
2. IAM -> Service accounts -> create one -> Keys -> add a **JSON key**. Keep the file out of
   the repo (e.g. `~/.config/snootsbot/drive-sa.json` on the laptop, `/opt/bots/snootsbot/secrets/drive-sa.json` on the VDS, chmod 600).
3. In Drive create a folder, share it with the service account's e-mail (Editor). The folder
   id is the tail of its URL.
4. `uv run tools/dbsync.py init`. A service account has no Drive quota, so on a normal "My
   Drive" folder it cannot create files: `init` then lists three empty files to create by hand
   (`stickers.db.gz`, `bot_data.db.gz`, `raw_captions.db.gz`); the account only updates them.
   On a Shared Drive nothing is needed.

## 2. Laptop
```
cp dbsync.example.toml dbsync.toml     # side = "laptop", folder_id, sa_file
uv run tools/dbsync.py status
uv run tools/dbsync.py sync stickers   # first run: uploads your stickers.db as the master copy
```
Adding packs: `dbsync.py pull bot_data` -> run the parser with `--requests-db data/bot_data.db`
-> `dbsync.py sync stickers`. (`dbsync.py all` does the three steps around the parser:
pull bot_data, sync stickers, back up raw_captions.) Do not run it while the parser is running.

## 3. VDS (once)
```
mkdir -p /opt/bots/snootsbot/{data,secrets}
# /opt/bots/snootsbot/.env : BOT_TOKEN=..., PANEL_PASSWORD=..., AUTO_APPROVE_PACKS=1 (optional)
# /opt/bots/snootsbot/secrets/drive-sa.json : the key
# /opt/bots/snootsbot/dbsync.toml : side = "vds", folder_id, sa_file,
#                                   stop_cmd = "systemctl stop snootsbot", start_cmd = "systemctl start snootsbot"
```
Then push to `main`: the workflow runs the tests, copies the code, installs the dependencies
(CPU torch), installs the systemd units and restarts the bot. The sync timer starts 2 minutes after the deploy (then every 10 min): on the fresh VDS it
downloads the master `stickers.db` from Drive by itself - so upload it from the laptop first
(section 2). The embedding model downloads into the HF cache on the first start (~470 MB).

GitHub secrets: `SERVER_HOST`, `SERVER_USER` (root, or a user allowed to run systemctl without
a password), `SSH_PRIVATE_KEY`.

## What the sync guarantees
* `stickers.db` is merged row by row against the last synced state: new stickers from the
  laptop and description edits made on the VDS (panel / approved suggestions) both survive.
  A row edited on both sides: the VDS version wins. Diverged ids (both sides inserted rows)
  abort the sync with an explanation instead of guessing.
* The bot is stopped only when the live `stickers.db` really has to change, and is started
  again even if the swap fails. The old file stays as `stickers.db.bak-*` (last 2 kept).
* `bot_data.db` is written only on the VDS, `raw_captions.db` only on the laptop; the other
  side refuses to push them (and the owner refuses to pull over its own data) without `--force`.
* Every transfer is a consistent sqlite snapshot, gzip-compressed, checked with md5 and
  `PRAGMA quick_check` before it is used.

# stickersbot

Inline search bot for Telegram sticker packs. Send `@bot query` in any chat and get
matching stickers as you type - the index understands plurals, fixes typos and mixes
two search channels (full-text + embeddings), plus per-user personalization.

```
                       ┌──────────────────────────── parser (local GPU) ───────────────────────┐
 base-stickerpacks.txt │  Bot API download → JoyCaption (vision LLM) → DeepSeek (JSON tags)   │
 + pack requests       │  → MiniLM embeddings → data/stickers.db  (append-only)               │
 from the admin panel  └──────────────────────────────────────────────────────────────────────┘

 bot/bot.py: query → FTS5 (bm25) ┐
            → cosine over 384d   ├─ RRF fusion → prefs/boosts/NSFW soft demotion
            sticker embeddings  ┘   → pack diversification → answerInlineQuery
```

## Indexing pipeline

`parser/parser.py` walks the pack list, downloads each sticker in memory (nothing is
written to disk), captions it with a vision LLM (JoyCaption via an OpenAI-compatible
server, e.g. llama.cpp), turns the caption into structured JSON with a second LLM
(OpenRouter): description, characters, actions, style tags, NSFW flag. Then
`paraphrase-multilingual-MiniLM-L12-v2` produces a 384-dim embedding; everything lands
in `data/stickers.db` (SQLite, FTS5 index kept in sync via `parser/textclean.py`).
The stages are wired as a LangGraph graph (`parser/graph.py`): `scan` fans out with
`Send`, then caption → postprocess → persist, each sticker its own branch.

The run is fully resumable: `file_unique_id` is the dedup key, raw captions are cached
in `data/raw_captions.db`, so a rerun skips whatever is already stored.

```bash
cd parser
pip install -r ../bot/requirements.txt pillow langgraph
echo "BOT_TOKEN=..." > .env
echo "OPENROUTER_API_KEY=..." >> .env
python parser.py --limit 50     # trial run (needs the JoyCaption server at JOY_URL)
```

## The bot

```bash
pip install -r bot/requirements.txt
python bot/bot.py
```

Reads `data/stickers.db` (index, loaded into memory once) and writes usage metrics,
favorites and moderation state to `data/bot_data.db`. Inline mode must be enabled in
BotFather (with inline feedback, so chosen results arrive as events).

- FTS channel: bm25 over description/characters/… with column weights, porter stemming,
  stop-word handling, spell correction of unknown words (Damerau-Levenshtein).
- Dense channel: cosine similarity over MiniLM embeddings.
- Ranking: RRF, favorite-pack boost, global pack boosts (admin panel), a small
  popularity prior from real clicks, soft NSFW demotion (an NSFW-intent query lifts
  it), at most 3 stickers per pack in the visible head.
- Results: `InlineQueryResultCachedSticker`; the result id is `stickers.id`, so
  chosen events are attributable even when Telegram serves its own cache.

Private chat: `/packs` (favorites with delete, popular packs), sending a sticker or
`t.me/addstickers/...` link adds the pack to the user's favorites and into the indexing
queue (10 new packs per day), sending a sticker shows an editable description card.
User text goes through `bot/texts.py` only - all strings in one place.

## Admin panel (optional)

`PANEL_PASSWORD=... python bot/bot.py` starts a dark-themed single-page panel on
`127.0.0.1:8081` (vanilla JS, no build step): DAU/MAU/CTR and daily chart, description
editing (re-embeds on save, audit log), user suggestions, pack requests, pack boosts.
Reach it via an ssh tunnel; auth is by password into an HttpOnly cookie.

## Layout

```
parser/   indexing pipeline (graph.py, parser.py, embedder.py, textclean.py, reindex.py, prompts)
bot/      self-contained search bot (search, metrics store, panel, private chat)
crawler/  collects pack links from a Telegram channel into parser/base-stickerpacks.txt
tools/    dbsync.py - laptop <-> Google Drive <-> VDS database sync (3-way row merge)
deploy/   systemd units, deployment notes
tests/    unittest suite (stdlib, no model download, no network)
data/     stickers.db, bot_data.db, raw_captions.db (created at runtime, not in git)
```

## Tests

```bash
python -m unittest discover -s tests
```

Uses a tiny hash-based fake embedder and a synthetic DB, so the files stay small and
CI never downloads the model. `python parser/reindex.py` rebuilds derived indexes of
an existing DB (FTS; `--reembed` also recomputes vectors) - run it with the bot stopped.

## Config

| Where | What |
|---|---|
| `.env` (next to `parser.py`/`bot.py`) | `BOT_TOKEN`, `OPENROUTER_API_KEY`, `PANEL_PASSWORD` |
| `JOY_URL`, `JOY_MODEL` | JoyCaption server endpoint / model id |
| `OPENROUTER_MODEL`, `PP_REASONING=off` | postprocessing model; disable reasoning |
| `PP_URL` | postprocessor endpoint (default OpenRouter). Point it at
  `http://127.0.0.1:11434/v1/chat/completions` + `OPENROUTER_MODEL=qwen2.5:14b` to run
  the postprocessor on a local model via Ollama (no API key needed) |
| `AUTO_APPROVE_PACKS=1` | index user packs without a panel click |
| `EMB_DEVICE`, `MODEL_DIR` | embedder device / local model copy |
| `dbsync.toml` | backup/sync config, see `dbsync.example.toml` |

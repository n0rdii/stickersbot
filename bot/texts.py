"""Every user-facing string of the private chat lives here, so the voice of the bot can be
tuned without touching the logic. Messages are sent with parse_mode=HTML: dynamic values
(pack titles, descriptions...) are ALWAYS passed through html.escape in dm.py, and only
<b>, <i>, <code> are used here.

House rules (tests/test_search.py enforces the first one):
  * never mention moderation, review, approval or an admin to the user - a pack/description
    that was sent is simply "added" / "saved";
  * cute, warm, a little silly (boop!), emojis welcome - but one clear next step per message.
"""

# ------------------------------------------------------------------ buttons
BTN_MY_PACKS = "⭐ My packs"
BTN_ADD = "➕ Add a pack"
BTN_POPULAR = "🔥 Popular packs"
BTN_BACK = "⬅️ My packs"
BTN_REMOVE = "🗑"
BTN_OPEN = "↗️"
BTN_CARD_FAV = "⭐ Add pack to favorites"
BTN_CARD_UNFAV = "🗑 Remove pack from favorites"
BTN_CARD_ADD = "➕ Add this pack to search"
BTN_CARD_EDIT = "✏️ Fix description"

# ------------------------------------------------------------------ start / help
START = (
    "🐾 <b>Hello!</b> I'm a bot made for furry stickers search! 👃✨\n\n"
    "🔍 <b>Finding stickers</b>\n"
    "In any chat type <code>@{bot} sleepy wolf</code> and I'll fetch the best matches! 🐺💤\n\n"
    "💬 <b>In here we can</b>\n"
    "⭐ /packs - pick your favorite packs, their stickers hop to the top of your searches!\n"
    "➕ Send me a sticker or a <code>t.me/addstickers/…</code> link - I'll take that whole pack into the search! 🎉\n"
    "✏️ Send me any sticker to correct the description, if you think something is wrong\n\n"
    "Boop! :3 What shall we do first?"
)

# ------------------------------------------------------------------ favorites
FAV_HEAD = ("⭐ <b>Your favorite packs</b>\n\n"
            "Stickers from these packs always hop to the top of your search results! 🐇💨")
FAV_EMPTY = ("\n\nIt's so empty in here… 🥺\n"
             "Tap ➕ to add a pack, or peek at what everybody likes 🔥")
FAV_FOOT = "\n\n🐾 tap a name to open the pack · 🗑 drops it from your list"
FAV_REMOVED = "Gone! 🗑 {name} left your favorites"
FAV_ADDED = "Added to favorites ⭐"
FAV_FULL = ("🙈 Your favorites list is full ({n} packs)! "
            "Drop a few with 🗑 and I'll happily take new ones.")

POPULAR_HEAD = ("🔥 <b>Popular packs</b>\n\n"
                "Tap a pack to add it to your favorites ⭐ - ↗️ opens it in Telegram.")
POPULAR_EMPTY = ("\n\nNothing new to suggest right now 🐾\n"
                 "You already have all the popular ones - great taste! 😋")

# ------------------------------------------------------------------ adding packs
ADD_PROMPT = (
    "➕ <b>Add a pack</b>\n\n"
    "Send me any of these and I'll take care of the rest:\n"
    "🖼 a sticker from the pack\n"
    "🔗 a <code>t.me/addstickers/…</code> link\n"
    "⌨️ the pack's short name\n\n"
    "/cancel to go back 🐾"
)
PACK_ADDED = (
    "🎉 <b>Yay!</b> <b>{title}</b> ({count} stickers) is added!\n\n"
    "🔎 Its stickers will start showing up in search a little later ✨\n"
    "⭐ I put it in your favorites too!"
)
PACK_ALREADY_THERE = (
    "🥳 <b>{title}</b> is already in my search - great taste! 😋\n"
    "⭐ It's in your favorites now."
)
PACK_NOT_FOUND = ("🥺 I can't find that pack. Check the link or the name and try again?\n"
                  "(it looks like <code>t.me/addstickers/<b>name</b></code>)")
PACK_NOT_STICKERS = ("🥺 That one is an emoji (or mask) pack - I only know regular stickers. "
                     "Got another one?")
PACK_LIMIT = "😵‍💫 Whoa, that's a lot of packs for one day! Let me catch my breath - try again tomorrow 💤"
PACK_TG_ERROR = "🙈 Telegram is being shy right now. Try again in a minute?"
PACK_BAD_NAME = ("🤔 That doesn't look like a pack name. It's letters, digits and underscores, "
                 "like in <code>t.me/addstickers/<b>name</b></code>. Try again? (/cancel to stop)")
NO_PACK = ("🥺 This sticker doesn't belong to a pack, so I can't look it up. "
           "Send one from a regular pack?")

# ------------------------------------------------------------------ sticker card
CARD_KNOWN = (
    "🐾 <b>Found it!</b> Sticker #{sid} from <b>{pack}</b>\n\n"
    "📝 <i>{description}</i>"
)
CARD_PACK_KNOWN = ("🐾 This exact sticker is new to me, but <b>{pack}</b> is already in my search! 🎉")
CARD_PACK_UNKNOWN = ("🔍 Ooh, I don't know <b>{pack}</b> yet!\n"
                     "Want me to take the whole pack into the search? 🐾")
CARD_EXPIRED = "Oops, I lost track of that one 🙈 Send the sticker again?"

# ------------------------------------------------------------------ descriptions
DESC_ASK = (
    "✏️ <b>What does this sticker really show?</b>\n\n"
    "Now: <i>{description}</i>\n\n"
    "Tell me in your own words - 10 to 300 characters, English please 🇬🇧\n"
    "Think of how you'd search for it! 🔍\n"
    "/cancel to skip"
)
DESC_BAD = "Hmm, I need 10 to 300 characters 🐾 Try again? (/cancel to skip)"
DESC_THANKS = "💖 Thank you! Got it (#{sug_id}) - you're the best snoot! 🐾"
DESC_ERROR = "🙈 Something went wrong on my side, please try again in a bit."

# ------------------------------------------------------------------ misc
CANCELLED = "Okie, cancelled! 🐾"
NOTHING_TO_CANCEL = "Nothing to cancel 🐾"
FALLBACK = ("Boop :3 I speak sticker!\n"
            "Send me a sticker or a pack link - or use me inline: "
            "<code>@{bot} your query</code> 🔍")
OLD_MENU = "This menu is from an older version 🐾 Send /packs for a fresh one!"


def all_texts() -> dict[str, str]:
    """Every string constant of this module (used by the tests)."""
    return {k: v for k, v in globals().items() if k.isupper() and isinstance(v, str)}

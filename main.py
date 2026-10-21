# -*- coding: utf-8 -*-
"""Private group-help bot (owner only, button controlled).
 - Owner manages groups (add / change / delete) with buttons in the bot DM.
 - Every group has its OWN join requirement: required channels (JOIN CHANNEL 1, 2... + VERIFY).
 - Bot text style: first letter bold, rest small-caps (ported from the like-bot)."""
import asyncio, copy, html, json, logging, os, re, sys, threading, time, uuid
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, ContextTypes,
                          ExtBot, MessageHandler, filters)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DATA_FILE = "data.json"

# ----- constants copied from the like-bot -----
VALID_STYLES = {"success", "primary", "danger"}      # green / blue / red
COLOR_LABELS = {"success": "🟢 GREEN", "primary": "🔵 BLUE", "danger": "🔴 RED"}
COLOR_CODES = {"g": "success", "b": "primary", "r": "danger"}
MAX_CHANNELS = 10
MAX_GROUPS = 10

# Channel buttons are FIXED: always BLUE (primary), name is automatic
# "𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 𝟭", "𝗝𝗢𝗜𝗡 𝗖𝗛𝗔𝗡𝗡𝗘𝗟 𝟮" ... and cannot be edited from /admin.
CHANNEL_BUTTON_STYLE = "primary"
DEFAULT_VERIFY_TEXT = "✅ 𝗩𝗘𝗥𝗜𝗙𝗬 𝗝𝗢𝗜𝗡"




def _sans_bold(text):
    """ASCII letters/digits -> mathematical sans-serif bold (𝗔𝗕𝗖 𝟭𝟮𝟯)."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D5EE + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def channel_button_name(number):
    return _sans_bold(f"JOIN CHANNEL {number}")




# ----- bot text style copied from the like-bot -----
_BOLD_CAPS = {chr(65 + i): chr(0x1D400 + i) for i in range(26)}
_SMALL_CAPS = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ"))
_STYLE_TOKEN = re.compile(r"((?:https?|tg)://\S+|(?<![A-Za-z0-9_])[/@][A-Za-z0-9_]+|\\.)|([A-Za-z\U0001D400-\U0001D419ᴀʙᴄᴅᴇғɢʜɪᴊᴋʟᴍɴᴏᴘǫʀᴛᴜᴠᴡʏᴢ]+)", re.I)
_HTML_SPLIT = re.compile(r"(<[^>]*>|&#?\w+;)")
_SKIP_TAG = re.compile(r"<(/?)(code|a|pre)\b", re.I)


def _style_word(w):
    # Already styled word (has non-ASCII small-caps/bold letters)? Leave it. Keeps sc() idempotent,
    # because plain "s" and "x" are also ASCII letters in this style.
    if not w.isascii():
        return w
    return _BOLD_CAPS[w[0].upper()] + "".join(_SMALL_CAPS[c.lower()] for c in w[1:])


# Invisible marker (WORD JOINER). Text between two markers is a user NAME and is never restyled,
# so "Murshalim Sk" stays exactly "Murshalim Sk".
NAME_MARK = "\u2060"


def _style_plain_raw(text):
    return _STYLE_TOKEN.sub(lambda m: m.group(1) if m.group(1) else _style_word(m.group(2)), text)


def _style_plain(text, state=None):
    if state is None:
        state = {"in": False}
    out = []
    for i, part in enumerate(text.split(NAME_MARK)):
        if i > 0:
            state["in"] = not state["in"]
            out.append(NAME_MARK)
        out.append(part if state["in"] else _style_plain_raw(part))
    return "".join(out)


def sc(text, html=True):
    """Convert normal text into the bot style. html=True keeps tags/entities and <code>/<a>/<pre> content untouched."""
    if not isinstance(text, str):
        return text
    if not html:
        return _style_plain(text)
    out, depth = [], 0
    state = {"in": False}
    for tok in _HTML_SPLIT.split(text):
        if not tok:
            continue
        if tok.startswith("<") and tok.endswith(">"):
            m = _SKIP_TAG.match(tok)
            if m:
                depth = max(0, depth - 1) if m.group(1) else depth + 1
            out.append(tok)
        elif tok.startswith("&") and tok.endswith(";"):
            out.append(tok)
        else:
            out.append(tok if depth else _style_plain(tok, state))
    return "".join(out)




# ----------------------------------------------------------------- storage
# Every group has its OWN join-gate settings.
GROUP_DEFAULTS = {
    "gate": True,                       # join gate ON/OFF for this group
    "channels": [],                     # required channels: [{"id","link","chat"}]
    "required_groups": [],              # required groups: [{"id","link","chat"}]
    "verify_text": DEFAULT_VERIFY_TEXT,
    "verify_color": "success",
    "verify_icon": "",                  # premium emoji id on the VERIFY button ("" = none)
    "delete_msg": True,                 # delete the message of a user who has not joined
}


def group_defaults():
    return copy.deepcopy(GROUP_DEFAULTS)


class Store:
    """JSON file: {"groups": {"<chat_id>": {title, type, added, + GROUP_DEFAULTS keys}}}"""

    def __init__(self):
        self.data = {"groups": {}}
        legacy = {}
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self.data["groups"] = raw.get("groups", {})
                legacy = raw.get("settings", {})      # older version had global settings
            except Exception:
                log.exception("could not read data.json")
        for g in self.data["groups"].values():
            for k, v in GROUP_DEFAULTS.items():
                if k not in g:
                    g[k] = copy.deepcopy(legacy.get(k, v)) if k != "gate" else v
            if g.get("verify_color") not in VALID_STYLES:
                g["verify_color"] = "success"

    @property
    def groups(self):
        return self.data["groups"]

    def save(self):
        try:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, DATA_FILE)
        except Exception:
            log.exception("could not write data.json")


db = Store()
sessions = {}          # owner's active "send me a value" wizard
joined_cache = {}      # user_id -> time of last successful full check (60 s)
prompts = {}           # (chat_id, user_id) -> (message_id, time) join prompt already shown
PROMPT_SECONDS = 600
_tasks = set()


# ----------------------------------------------------------------- styled bot / buttons
def _styled(text, kwargs):
    if not isinstance(text, str):
        return text
    return sc(text, kwargs.get("parse_mode") == "HTML")


class StyledBot(ExtBot):
    """Every message / edit / popup goes through the bot text style sc()."""

    async def send_message(self, chat_id, text, *args, **kwargs):
        return await super().send_message(chat_id, _styled(text, kwargs), *args, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        return await super().edit_message_text(_styled(text, kwargs), *args, **kwargs)

    async def answer_callback_query(self, callback_query_id, text=None, *args, **kwargs):
        if isinstance(text, str):
            text = sc(text, False)
        return await super().answer_callback_query(callback_query_id, text, *args, **kwargs)


def btn(text, url=None, data=None, style=None, icon=None):
    """Inline button: styled text, optional colour (success/primary/danger) and premium icon."""
    extra = {}
    if style in VALID_STYLES:
        extra["style"] = style
    if icon:
        extra["icon_custom_emoji_id"] = icon
    return IKB(sc(text, False), url=url, callback_data=data, api_kwargs=extra or None)


def later(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _del_after(msg, sec):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


def full_name(user):
    parts = [p for p in (user.first_name, getattr(user, "last_name", None)) if p]
    return " ".join(parts).strip() or "USER"


def name_text(user):
    """Plain (non-clickable) name that the text style never restyles."""
    name = escape(full_name(user)).replace("@", "@\u2060")
    return NAME_MARK + name + NAME_MARK


def say(m, text, **kw):
    return m.reply_text(text, parse_mode="HTML", disable_web_page_preview=True, **kw)


# ----------------------------------------------------------------- chat helpers
def parse_ref(ref):
    """id / @username / t.me link -> value accepted by bot.get_chat()."""
    ref = ref.strip()
    if re.fullmatch(r"-?\d+", ref):
        return int(ref)
    mt = re.match(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(.+)$", ref, re.I)
    if mt:
        part = mt.group(1).split("?")[0].strip("/")
        if part.startswith("+") or part.lower().startswith("joinchat"):
            raise ValueError("private_link")
        bits = part.split("/")
        if bits[0] == "c" and len(bits) > 1 and bits[1].isdigit():
            return int("-100" + bits[1])
        return "@" + bits[0]
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", ref):
        return ref if ref.startswith("@") else "@" + ref
    raise ValueError("bad_ref")


def derive_chat_from_link(link):
    m = re.match(r"^https?://t\.me/([A-Za-z][A-Za-z0-9_]{4,31})/?$", link.strip())
    return ("@" + m.group(1)) if m else ""


def normalize_chat(value):
    v = value.strip()
    d = derive_chat_from_link(v)
    if d:
        return d
    if v.lstrip("-").isdigit():
        return v
    if re.match(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$", v):
        return "@" + v.lstrip("@")
    return ""


def valid_link(value):
    return value.startswith(("https://", "http://", "tg://"))


def err_text(e):
    s = str(e)
    if s == "private_link":
        return ("❌ Private invite links (t.me/+...) cannot be converted to a chat ID by a bot. "
                "Send the numeric ID (-100...) or forward a message from that chat.")
    if s == "bad_ref":
        return "❌ Send a chat ID (-100...), @username or a t.me link."
    if s == "not_group":
        return "❌ That is not a group or channel."
    if s == "is_channel":
        return "❌ That is a channel. Add channels under 📢 Required Channels."
    if s == "not_channel":
        return "❌ That is not a channel."
    return f"❌ Could not find that chat. Make sure the bot is added there (or the chat is public).\n<i>{escape(s)}</i>"


async def chat_from_message(bot, m):
    """Chat from a forwarded post / anonymous-admin forward, or from the typed id/link."""
    fo = getattr(m, "forward_origin", None)
    if fo is not None:
        ch = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        if ch is not None:
            return ch
    chat = await bot.get_chat(parse_ref((m.text or "").strip()))
    if chat.type == ChatType.PRIVATE:
        raise ValueError("not_group")
    return chat


async def bot_status(bot, chat_id):
    try:
        cm = await bot.get_chat_member(chat_id, bot.id)
    except Exception:
        return "❌ bot not in chat"
    if cm.status == "creator":
        return "✅ admin"
    if cm.status == "administrator":
        return "✅ admin" if getattr(cm, "can_delete_messages", True) is not False else "⚠️ admin (no delete right)"
    if cm.status == "member":
        return "⚠️ member (not admin)"
    return "❌ bot not in chat"


_bot_admin_cache = {}


async def bot_is_admin(bot, chat_id):
    ts, val = _bot_admin_cache.get(chat_id, (0, False))
    if time.time() - ts < 300:
        return val
    try:
        cm = await bot.get_chat_member(chat_id, bot.id)
        val = cm.status in ("administrator", "creator")
    except Exception:
        val = False
    _bot_admin_cache[chat_id] = (time.time(), val)
    return val


async def check_bot_access(bot, chat):
    """The bot must be an admin of the required channel, otherwise it cannot verify members."""
    try:
        target = int(chat) if str(chat).lstrip("-").isdigit() else chat
        member = await bot.get_chat_member(target, bot.id)
        if member.status not in ("administrator", "creator"):
            return False, ("Bot is not admin in that channel. Make the bot admin first, "
                           "then send the channel @username or ID again.")
        return True, ""
    except Exception as e:
        log.warning("Channel access check failed for %s: %s", chat, e)
        return False, ("Cannot access that channel. Check the @username / ID and make sure "
                       "the bot is admin there. Send it again.")


# ----------------------------------------------------------------- REQUIRED CHANNELS (JOIN GATE, per group)
async def is_member_of(bot, chat, user_id):
    try:
        target = int(chat) if str(chat).lstrip("-").isdigit() else chat
        m = await bot.get_chat_member(target, user_id)
    except Exception as e:
        # Misconfigured channel (bot removed / not admin): do not lock everybody out.
        log.warning("Membership check failed for %s: %s", chat, e)
        return True
    if m.status in ("creator", "administrator", "member"):
        return True
    if m.status == "restricted":
        return bool(getattr(m, "is_member", True))
    return False


async def missing_channels(bot, g, user_id):
    """Required channels of THIS group that the user has NOT joined yet (fresh check)."""
    if user_id == OWNER_ID:
        return []
    out = []
    for ch in g["channels"]:
        if ch.get("chat") and not await is_member_of(bot, ch["chat"], user_id):
            out.append(ch)
    return out


async def missing_required_groups(bot, g, user_id):
    if user_id == OWNER_ID:
        return []
    out = []
    for item in g.get("required_groups", []):
        if item.get("chat") and not await is_member_of(bot, item["chat"], user_id):
            out.append(item)
    return out


async def all_missing_requirements(bot, g, user_id):
    return await missing_channels(bot, g, user_id), await missing_required_groups(bot, g, user_id)



def verify_button(g, user_id, use_icon=True):
    return btn(g["verify_text"], data=f"v:{user_id}", style=g["verify_color"],
               icon=(g.get("verify_icon") if use_icon else ""))


def group_button_name(number):
    return _sans_bold(f"JOIN GROUP {number}")


def join_markup(g, user_id, missing, missing_groups=None, use_icon=True):
    missing_groups = missing_groups or []
    rows = []
    for ch in missing:
        n = next((i for i, x in enumerate(g["channels"], 1) if x["id"] == ch["id"]), 1)
        rows.append([btn(channel_button_name(n), url=ch["link"], style="primary")])
    for item in missing_groups:
        n = next((i for i, x in enumerate(g.get("required_groups", []), 1) if x["id"] == item["id"]), 1)
        rows.append([btn(group_button_name(n), url=item["link"], style="primary")])
    rows.append([verify_button(g, user_id, use_icon)])
    return InlineKeyboardMarkup(rows)


async def send_join_prompt(bot, chat_id, g, user, missing, missing_groups=None):
    missing_groups = missing_groups or []
    has_channels = bool(g.get("channels"))
    has_groups = bool(g.get("required_groups"))
    if has_channels and has_groups:
        requirement_text = "You must join all the required channels or groups to continue messaging"
    elif has_channels:
        requirement_text = "You must join all the required channels to continue messaging"
    else:
        requirement_text = "You must join all the required groups to continue messaging"
    text = (f"<blockquote>👋 Hello {name_text(user)}\n\n"
            f"⚠️ {requirement_text}\n\n"
            "👇 Join the buttons below then tap on Verify join button</blockquote>")
    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      reply_markup=join_markup(g, user.id, missing, missing_groups))
    except BadRequest as e:
        if not g.get("verify_icon"):
            raise
        log.warning("Join prompt failed with premium icon, retrying without it: %s", e)
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      reply_markup=join_markup(g, user.id, missing, missing_groups, use_icon=False))


async def gate_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every message in the owner's groups. Uses THIS group's own settings."""
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not m or not user or user.is_bot or m.sender_chat or getattr(m, "is_automatic_forward", False):
        return
    g = db.groups.get(str(chat.id))
    if not g or not g["gate"] or not (g["channels"] or g.get("required_groups")):
        return
    # Always check current membership for every incoming message. Do not trust a
    # previous verification cache because a user may leave a required chat later.
    joined_cache.pop((chat.id, user.id), None)
    if not await bot_is_admin(context.bot, chat.id):
        return
    missing, missing_groups = await all_missing_requirements(context.bot, g, user.id)
    if not missing and not missing_groups:
        return
    # Keep one visible join prompt per user. Replace the previous prompt so a fresh
    # prompt is shown after every blocked message instead of silently throttling it.
    key = (chat.id, user.id)
    old = prompts.get(key)
    if old:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=old[0])
        except Exception:
            pass
        prompts.pop(key, None)

    # Send the join prompt first, then delete the user's message. This makes the
    # prompt attempt independent from whether deleting the incoming message succeeds.
    try:
        sent = await send_join_prompt(context.bot, chat.id, g, user, missing, missing_groups)
        prompts[key] = (sent.message_id, time.time())
    except Exception:
        log.exception("could not send join prompt to chat %s for user %s", chat.id, user.id)

    if g["delete_msg"]:
        try:
            await m.delete()
        except Exception:
            log.exception("could not delete blocked message in chat %s", chat.id)


async def on_verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        target = int((q.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await q.answer()
        return
    if q.from_user.id != target:       # only the user this prompt was made for
        await q.answer("❌ This button is not for you.", show_alert=True)
        return
    chat_id = q.message.chat.id
    g = db.groups.get(str(chat_id))
    if not g:
        await q.answer()
        return
    joined_cache.pop((chat_id, target), None)
    missing, missing_groups = await all_missing_requirements(context.bot, g, target)
    if missing or missing_groups:
        try:
            try:
                await q.edit_message_reply_markup(reply_markup=join_markup(g, target, missing, missing_groups))
            except BadRequest as e:
                if "not modified" in str(e).lower() or not g.get("verify_icon"):
                    raise
                await q.edit_message_reply_markup(reply_markup=join_markup(g, target, missing, missing_groups, use_icon=False))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("Verify refresh failed: %s", e)
        await q.answer("❌ You have not joined all required channels yet.", show_alert=True)
        return
    joined_cache.pop((chat_id, target), None)
    prompts.pop((chat_id, target), None)
    await q.answer("✅ Verified successfully")
    has_channels = bool(g.get("channels"))
    has_groups = bool(g.get("required_groups"))
    if has_channels and has_groups:
        thank_you_text = "Thank you for joining all the required channels or groups"
    elif has_channels:
        thank_you_text = "Thank you for joining all the required channels"
    else:
        thank_you_text = "Thank you for joining all the required groups"
    try:
        await q.edit_message_text(
            f"<blockquote>👋 Hello {name_text(q.from_user)}\n\n"
            f"🎉 Welcome to \n{escape(q.message.chat.title or 'the group')}\n\n"
            f"❤️ {thank_you_text}\n\n"
            "✅ You are verified You can now continue messaging</blockquote>",
            parse_mode="HTML")
        later(_del_after(q.message, 10))
    except Exception:
        pass


# ----------------------------------------------------------------- group list helpers
def save_group(chat):
    gid = str(chat.id)
    new = gid not in db.groups
    if new:
        db.groups[gid] = {"title": chat.title or gid, "type": chat.type, "added": int(time.time()),
                          **group_defaults()}
    else:
        db.groups[gid]["title"] = chat.title or gid
        db.groups[gid]["type"] = chat.type
    db.save()
    return new


def replace_group(old_gid, chat):
    """Swap one group for another, keeping its position AND all its settings."""
    new_gid = str(chat.id)
    items = list(db.groups.items())
    db.groups.clear()
    for k, v in items:
        if k == old_gid:
            v["title"], v["type"] = chat.title or new_gid, chat.type
            db.groups[new_gid] = v
        elif k != new_gid:
            db.groups[k] = v
    db.save()


def new_channel(gid, link, chat):
    ch = {"id": uuid.uuid4().hex[:8], "link": link, "chat": chat}
    db.groups[gid]["channels"].append(ch)
    db.save()
    for k in [k for k in joined_cache if k[0] == int(gid)]:
        joined_cache.pop(k, None)
    return ch


def find_channel(gid, cid):
    return next((c for c in db.groups.get(gid, {}).get("channels", []) if c["id"] == cid), None)


def channel_number(gid, cid):
    return next((i for i, c in enumerate(db.groups.get(gid, {}).get("channels", []), 1) if c["id"] == cid), 0)


async def add_required_channel(bot, owner_chat_id, gid, chan):
    """Add channel `chan` (Chat) as a required channel of group `gid` (from the 'bot added' button)."""
    ok, err = await check_bot_access(bot, str(chan.id))
    if not ok:
        await bot.send_message(owner_chat_id, "❌ " + err)
        return
    link = f"https://t.me/{chan.username}" if chan.username else ""
    if not link:
        try:
            link = await bot.export_chat_invite_link(chan.id)
        except Exception:
            link = ""
    if link:
        ch = new_channel(gid, link, str(chan.id))
        await send_panel(bot, owner_chat_id, channel_panel(gid, ch["id"]), "✅ Channel Added\n\n")
    else:
        sessions[OWNER_ID] = {"action": "add", "gid": gid, "step": "link", "data": {"chat": str(chan.id)}}
        await bot.send_message(owner_chat_id, "This channel is private. Send its invite link (https://t.me/+...).",
                               reply_markup=cancel_markup(f"gc:{gid}"))


# ----------------------------------------------------------------- panels (owner, buttons only)
def onoff(v):
    return "ON ✅" if v else "OFF ⛔"


def main_panel():
    text = ("⚙️ <b>Control Panel</b>\n\n"
            f"📋 <b>Groups:</b> {len(db.groups)}\n\n"
            "Open <b>My Groups</b>, pick a group and control its join requirement.")
    m = InlineKeyboardMarkup([
        [btn("📋 My Groups", data="m:groups", style="primary"), btn("➕ Add Group", data="m:addg", style="success")],
        [btn("❌ Close", data="m:close", style="danger")],
    ])
    return text, m


async def groups_panel(bot):
    if db.groups:
        lines = []
        for i, (gid, g) in enumerate(db.groups.items(), 1):
            st = await bot_status(bot, int(gid))
            lines.append(f"<b>{i}.</b> {escape(g['title'])}\n    {st} · Gate {onoff(g['gate'])} · 📢 {len(g['channels'])}")
        body = "\n".join(lines)
    else:
        body = "No groups added yet."
    text = "📋 <b>My Groups</b>\nTap a group to control it.\n\n" + body
    rows = [[btn(f"{i}. {g['title'][:30]}", data=f"g:{gid}", style="primary")]
            for i, (gid, g) in enumerate(db.groups.items(), 1)]
    rows.append([btn("➕ Add Group", data="m:addg", style="success")])
    rows.append([btn("⬅️ Back", data="m:main")])
    return text, InlineKeyboardMarkup(rows)


async def group_panel(bot, gid):
    g = db.groups.get(gid)
    if not g:
        return await groups_panel(bot)
    st = await bot_status(bot, int(gid))
    n = len(g["channels"])
    warn = "" if n else "\n⚠️ No required channels yet, so the gate does nothing. Add them in 📢 Required Channels."
    text = (f"📋 <b>{escape(g['title'])}</b>\n<code>{gid}</code>\n\n"
            f"Bot: {st}\n"
            f"🔒 <b>Join Gate:</b> {onoff(g['gate'])}\n"
            f"📢 <b>Required Channels:</b> {n}\n"
            f"✅ <b>Verify Button:</b> <code>{escape(g['verify_text'])}</code>\n"
            f"🗑 <b>Delete Unjoined User's Msg:</b> {onoff(g['delete_msg'])}{warn}")
    m = InlineKeyboardMarkup([
        [btn(f"🔒 Join Gate: {onoff(g['gate'])}", data=f"gt:{gid}", style="success" if g["gate"] else "danger")],
        [btn("📢 Required Channels", data=f"gc:{gid}", style="primary")],
        [btn("👥 Required Groups", data=f"gg:{gid}", style="primary")],
        [btn("✅ Verify Button", data=f"gv:{gid}", style="success")],
        [btn(f"🗑 Delete Unjoined Msg: {onoff(g['delete_msg'])}", data=f"gm:{gid}")],
        [btn("✏️ Change Group", data=f"ge:{gid}", style="primary"), btn("🗑 Delete Group", data=f"gd:{gid}", style="danger")],
        [btn("⬅️ Back", data="m:groups")],
    ])
    return text, m


def channels_panel(gid):
    g = db.groups.get(gid)
    if not g:
        return main_panel()
    chans = g["channels"]
    if chans:
        lines = "\n".join(f"{i}. {channel_button_name(i)} — <code>{escape(c['chat'] or '-')}</code>"
                          for i, c in enumerate(chans, 1))
    else:
        lines = "No channels added yet."
    text = (f"📢 <b>Required Channels</b>\n{escape(g['title'])}\n"
            "Users must join all of these before they can message in this group.\n"
            "⚠️ The bot must be admin in every channel.\n"
            "🔒 Button name / colour are fixed. You can only add or delete.\n\n" + lines)
    rows = [[btn("📢 " + channel_button_name(i), data=f"c:{gid}:{c['id']}", style=CHANNEL_BUTTON_STYLE)]
            for i, c in enumerate(chans, 1)]
    if len(chans) < MAX_CHANNELS:
        rows.append([btn("➕ Add Channel", data=f"gca:{gid}", style="success")])
    rows.append([btn("⬅️ Back", data=f"g:{gid}")])
    return text, InlineKeyboardMarkup(rows)


def required_groups_panel(gid):
    g = db.groups.get(gid)
    if not g:
        return main_panel()
    items = g.get("required_groups", [])
    lines = "\\n".join(f"{i}. {group_button_name(i)} — <code>{escape(x['chat'] or '-')}</code>"
                       for i, x in enumerate(items, 1)) if items else "No required groups added yet."
    text = (f"👥 <b>Required Groups</b>\\n{escape(g['title'])}\\n"
            "Users must join every listed group before messaging. The bot must be in each required group (admin recommended).\\n\\n" + lines)
    rows = [[btn("👥 " + group_button_name(i), data=f"gr:{gid}:{x['id']}", style="primary")]
            for i, x in enumerate(items, 1)]
    if len(items) < MAX_GROUPS:
        rows.append([btn("➕ Add Group Requirement", data=f"gra:{gid}", style="success")])
    rows.append([btn("⬅️ Back", data=f"g:{gid}")])
    return text, InlineKeyboardMarkup(rows)


def required_group_panel(gid, rid):
    items = db.groups.get(gid, {}).get("required_groups", [])
    item = next((x for x in items if x["id"] == rid), None)
    if not item:
        return required_groups_panel(gid)
    n = next((i for i, x in enumerate(items, 1) if x["id"] == rid), 0)
    text = (f"👥 <b>Required Group Button</b>\\n\\nName: {group_button_name(n)}\\n"
            f"Link: <code>{escape(item['link'])}</code>\\nGroup ID: <code>{escape(item['chat'] or '-')}</code>")
    return text, InlineKeyboardMarkup([[btn("🗑 Delete Group Requirement", data=f"grd:{gid}:{rid}", style="danger")],
                                       [btn("⬅️ Back", data=f"gg:{gid}")] ])


def new_required_group(gid, link, chat):
    item = {"id": uuid.uuid4().hex[:8], "link": link, "chat": chat}
    db.groups[gid].setdefault("required_groups", []).append(item)
    db.save()
    for k in [k for k in joined_cache if k[0] == int(gid)]:
        joined_cache.pop(k, None)
    return item


def channel_panel(gid, cid):
    ch = find_channel(gid, cid)
    if not ch:
        return channels_panel(gid)
    text = ("📢 <b>Channel Button</b>\n\n"
            f"Name: {channel_button_name(channel_number(gid, cid))}\n"
            f"Link: <code>{escape(ch['link'])}</code>\n"
            f"Channel ID/Username: <code>{escape(ch['chat'] or 'Not Set')}</code>\n"
            f"Colour: {COLOR_LABELS[CHANNEL_BUTTON_STYLE]} (Fixed)")
    m = InlineKeyboardMarkup([[btn("🗑 Delete Channel", data=f"cd:{gid}:{cid}", style="danger")],
                              [btn("⬅️ Back", data=f"gc:{gid}")]])
    return text, m


def verify_panel(gid):
    g = db.groups.get(gid)
    if not g:
        return main_panel()
    icon = g.get("verify_icon") or ""
    text = (f"✅ <b>Verify Button</b>\n{escape(g['title'])}\n"
            "Shown under the channel buttons when a user has not joined.\n\n"
            f"Text: <code>{escape(g['verify_text'])}</code>\n"
            f"Colour: {COLOR_LABELS[g['verify_color']]}\n"
            f"💎 Premium Emoji: {'✅ Set' if icon else 'Not Set'}\n\n"
            "💡 To set a premium emoji: Edit Text, then send a message that has that emoji.")
    rows = [[btn("✏️ Edit Text", data=f"gvt:{gid}", style="success"), btn("🎨 Edit Colour", data=f"gvc:{gid}", style="success")]]
    if icon:
        rows.append([btn("🗑 Remove Premium Emoji", data=f"gvi:{gid}", style="danger")])
    rows.append([btn("⬅️ Back", data=f"g:{gid}")])
    return text, InlineKeyboardMarkup(rows)


def color_panel(gid):
    m = InlineKeyboardMarkup([
        [btn("🟢 Green", data=f"col:{gid}:g", style="success"), btn("🔵 Blue", data=f"col:{gid}:b", style="primary"),
         btn("🔴 Red", data=f"col:{gid}:r", style="danger")],
        [btn("⬅️ Back", data=f"gv:{gid}")]])
    return "🎨 <b>Choose Button Colour:</b>", m


def cancel_markup(back="m:main"):
    return InlineKeyboardMarkup([[btn("⬅️ Cancel", data=back)]])


async def show(q, panel, prefix=""):
    text, markup = panel
    try:
        await q.edit_message_text(prefix + text, parse_mode="HTML", reply_markup=markup,
                                  disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.error("Panel edit failed: %s", e)


async def send_panel(bot, chat_id, panel, prefix=""):
    text, markup = panel
    await bot.send_message(chat_id, prefix + text, parse_mode="HTML", reply_markup=markup,
                           disable_web_page_preview=True)


# ----------------------------------------------------------------- owner: buttons
async def on_owner_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID or q.message.chat.type != ChatType.PRIVATE:
        await q.answer("Not authorized.", show_alert=True)
        return
    uid, chat_id, bot = q.from_user.id, q.message.chat.id, context.bot
    parts = (q.data or "").split(":")
    kind = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    sessions.pop(uid, None)

    if kind == "add":        # "bot was added" notification (group)
        try:
            chat = await bot.get_chat(int(arg))
            new = save_group(chat)
            await q.answer("Added" if new else "Already in list")
            await show(q, await group_panel(bot, str(chat.id)), "✅ Group Added\n\n" if new else "")
        except Exception as e:
            await q.answer(f"Failed: {e}", show_alert=True)
        return

    if kind == "rch":        # "bot was added" notification (channel) -> pick the group it is required for
        if not db.groups:
            await q.answer("Add a group first (Add Group), then add this channel to it.", show_alert=True)
            return
        await q.answer()
        try:
            chan = await bot.get_chat(int(arg))
        except Exception as e:
            await bot.send_message(chat_id, f"❌ {escape(str(e))}", parse_mode="HTML")
            return
        if len(db.groups) == 1:
            await add_required_channel(bot, chat_id, next(iter(db.groups)), chan)
            return
        rows = [[btn(f"{i}. {g['title'][:30]}", data=f"rchg:{chan.id}:{gid}", style="primary")]
                for i, (gid, g) in enumerate(db.groups.items(), 1)]
        rows.append([btn("⬅️ Cancel", data="m:main")])
        await bot.send_message(chat_id, f"📢 Required for which group?\n<b>{escape(chan.title)}</b>",
                               parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return
    if kind == "rchg":
        await q.answer()
        try:
            chan = await bot.get_chat(int(arg))
        except Exception as e:
            await bot.send_message(chat_id, f"❌ {escape(str(e))}", parse_mode="HTML")
            return
        if arg2 in db.groups:
            await add_required_channel(bot, chat_id, arg2, chan)
        return

    await q.answer()

    if kind == "m":
        if arg == "main":
            await show(q, main_panel())
        elif arg == "close":
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
        elif arg == "groups":
            await show(q, await groups_panel(bot))
        elif arg == "addg":
            sessions[uid] = {"action": "addg"}
            await bot.send_message(
                chat_id,
                "➕ <b>Add Group</b>\n\nSend the group ID (-100...), @username or t.me link.\n"
                "💡 Easier: add the bot to the group as admin and I will send you an Add button.",
                parse_mode="HTML", reply_markup=cancel_markup())
        return

    # ---- everything below belongs to one group: arg = group id ----
    gid = arg
    g = db.groups.get(gid)
    if kind in ("g", "gt", "gm", "ge", "gd", "gdy", "gc", "gca", "gg", "gra", "gr", "grd", "gv", "gvt", "gvc", "gvi", "c", "cd", "cdy", "col") and not g:
        await show(q, await groups_panel(bot))
        return

    if kind == "g":
        await show(q, await group_panel(bot, gid))
    elif kind == "gt":
        g["gate"] = not g["gate"]
        db.save()
        await show(q, await group_panel(bot, gid))
    elif kind == "gm":
        g["delete_msg"] = not g["delete_msg"]
        db.save()
        await show(q, await group_panel(bot, gid))
    elif kind == "ge":
        sessions[uid] = {"action": "editg", "gid": gid}
        await bot.send_message(chat_id, "✏️ Send the NEW group ID (-100...), @username or t.me link.\n"
                                        "All settings of this group are kept.", reply_markup=cancel_markup(f"g:{gid}"))
    elif kind == "gd":
        m = InlineKeyboardMarkup([[btn("✅ Yes, Delete", data=f"gdy:{gid}", style="danger"),
                                   btn("⬅️ Cancel", data=f"g:{gid}")]])
        await show(q, (f"🗑 <b>Remove {escape(g['title'])} and all its settings?</b>", m))
    elif kind == "gdy":
        db.groups.pop(gid, None)
        db.save()
        await show(q, await groups_panel(bot), "🗑 Removed\n\n")

    # ---- required channels of this group ----
    elif kind == "gc":
        await show(q, channels_panel(gid))
    elif kind == "gca":
        if len(g["channels"]) >= MAX_CHANNELS:
            await bot.send_message(chat_id, f"❌ Maximum {MAX_CHANNELS} channels allowed.")
            return
        sessions[uid] = {"action": "add", "gid": gid, "step": "link", "data": {}}
        await bot.send_message(
            chat_id,
            f"➕ <b>Add Channel</b> for {escape(g['title'])}\n\nStep 1/2: send the channel join link (https://t.me/...).\n"
            "Button name and colour are automatic.\n"
            "💡 Or just add the bot to the channel as admin and tap the button I send you.",
            parse_mode="HTML", reply_markup=cancel_markup(f"gc:{gid}"))
    elif kind == "c":
        await show(q, channel_panel(gid, arg2))
    elif kind == "cd":
        m = InlineKeyboardMarkup([[btn("✅ Yes, Delete", data=f"cdy:{gid}:{arg2}", style="danger"),
                                   btn("⬅️ Cancel", data=f"c:{gid}:{arg2}")]])
        await show(q, ("🗑 <b>Delete this channel button?</b>", m))
    elif kind == "cdy":
        g["channels"] = [c for c in g["channels"] if c["id"] != arg2]
        db.save()
        for k in [k for k in joined_cache if k[0] == int(gid)]:
            joined_cache.pop(k, None)
        await show(q, channels_panel(gid), "🗑 Deleted\n\n")

    # ---- required groups of this group ----
    elif kind == "gg":
        await show(q, required_groups_panel(gid))
    elif kind == "gra":
        if len(g.get("required_groups", [])) >= MAX_GROUPS:
            await bot.send_message(chat_id, f"❌ Maximum {MAX_GROUPS} required groups allowed.")
            return
        sessions[uid] = {"action": "addrg", "gid": gid, "step": "link", "data": {}}
        await bot.send_message(chat_id,
            f"➕ <b>Add Required Group</b> for {escape(g['title'])}\\n\\n"
            "Step 1/2: send the group's public link (https://t.me/...) or invite link.\\n"
            "For private groups, you will also need to send the numeric group ID (-100...).",
            parse_mode="HTML", reply_markup=cancel_markup(f"gg:{gid}"))
    elif kind == "gr":
        await show(q, required_group_panel(gid, arg2))
    elif kind == "grd":
        g["required_groups"] = [x for x in g.get("required_groups", []) if x["id"] != arg2]
        db.save()
        for k in [k for k in joined_cache if k[0] == int(gid)]:
            joined_cache.pop(k, None)
        await show(q, required_groups_panel(gid), "🗑 Deleted\\n\\n")

    # ---- verify button of this group ----
    elif kind == "gv":
        await show(q, verify_panel(gid))
    elif kind == "gvc":
        await show(q, color_panel(gid))
    elif kind == "gvi":
        g["verify_icon"] = ""
        db.save()
        await show(q, verify_panel(gid), "✅ Updated\n\n")
    elif kind == "gvt":
        sessions[uid] = {"action": "vtxt", "gid": gid}
        await bot.send_message(
            chat_id,
            "✏️ Send the new VERIFY button text.\n💎 You can also send a message with a premium emoji. "
            "It will be used on the button.", reply_markup=cancel_markup(f"gv:{gid}"))
    elif kind == "col":
        style = COLOR_CODES.get(arg2)
        if style:
            g["verify_color"] = style
            db.save()
            await show(q, verify_panel(gid), "✅ Colour Updated\n\n")


# ----------------------------------------------------------------- owner: typed values (wizards)
def extract_custom_emoji(m):
    """(text without premium emojis, first premium emoji id or ''). Offsets are UTF-16 units."""
    text = m.text or ""
    ents = [e for e in (m.entities or []) if e.type == "custom_emoji" and getattr(e, "custom_emoji_id", None)]
    if not ents:
        return text.strip(), ""
    raw = text.encode("utf-16-le")
    out, pos = bytearray(), 0
    for s_, e_ in sorted((e.offset * 2, (e.offset + e.length) * 2) for e in ents):
        out += raw[pos:s_]
        pos = e_
    out += raw[pos:]
    cleaned = re.sub(r"[ \t]{2,}", " ", out.decode("utf-16-le")).strip()
    return cleaned, str(ents[0].custom_emoji_id)


async def handle_session(update: Update, context: ContextTypes.DEFAULT_TYPE, sess):
    m, bot = update.effective_message, context.bot
    uid, chat_id = update.effective_user.id, m.chat_id
    action = sess["action"]
    value = (m.text or "").strip()

    def done():
        sessions.pop(uid, None)

    if action in ("addg", "editg"):
        try:
            chat = await chat_from_message(bot, m)
            if chat.type == ChatType.CHANNEL:
                raise ValueError("is_channel")
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                raise ValueError("not_group")
        except Exception as e:
            await m.reply_text(err_text(e), parse_mode="HTML")
            return
        done()
        if action == "addg":
            new = save_group(chat)
            await send_panel(bot, chat_id, await group_panel(bot, str(chat.id)),
                             "✅ Group Added\n\n" if new else "♻️ Already in list\n\n")
        else:
            replace_group(sess["gid"], chat)
            await send_panel(bot, chat_id, await group_panel(bot, str(chat.id)), "✏️ Group Updated\n\n")
        return

    if action == "addrg":
        gid = sess["gid"]
        if gid not in db.groups:
            done()
            return
        d = sess["data"]
        if sess["step"] == "link":
            if not valid_link(value):
                await m.reply_text("❌ Invalid link. Use https://, http:// or tg://.")
                return
            d["link"] = value
            derived = derive_chat_from_link(value)
            if derived:
                ok, err = await check_bot_access(bot, derived)
                if ok:
                    item = new_required_group(gid, value, derived)
                    done()
                    await send_panel(bot, chat_id, required_group_panel(gid, item["id"]), "✅ Group Requirement Added\\n\\n")
                    return
            sess["step"] = "chat"
            await m.reply_text("Step 2/2: send the group's numeric ID (-100...) or @username. Make sure the bot is a member/admin there.")
            return
        chat = normalize_chat(value)
        if not chat:
            await m.reply_text("❌ Send a valid @username or numeric ID (-100...).")
            return
        ok, err = await check_bot_access(bot, chat)
        if not ok:
            await m.reply_text("❌ " + err)
            return
        item = new_required_group(gid, d["link"], chat)
        done()
        await send_panel(bot, chat_id, required_group_panel(gid, item["id"]), "✅ Group Requirement Added\\n\\n")
        return

    if action == "vtxt":
        icon = ""
        value, icon = extract_custom_emoji(m)
        if not value or len(value) > 64:
            await m.reply_text("❌ Button text must be between 1 and 64 characters.")
            return
        g = db.groups.get(sess["gid"])
        if not g:
            done()
            return
        g["verify_text"] = value
        g["verify_icon"] = icon
        db.save()
        done()
        await send_panel(bot, chat_id, verify_panel(sess["gid"]), "✅ Updated\n\n")
        return

    if action == "add":
        gid = sess["gid"]
        if gid not in db.groups:
            done()
            return
        d = sess["data"]
        step = sess["step"]
        if step == "link":
            fo = getattr(m, "forward_origin", None)
            fch = (getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)) if fo else None
            if fch is not None and getattr(fch, "username", None):
                value = f"https://t.me/{fch.username}"
            if not valid_link(value):
                await m.reply_text("❌ Invalid link. Use https://, http:// or tg://.")
                return
            d["link"] = value
            if d.get("chat"):                      # id already known (from notification)
                ch = new_channel(gid, value, d["chat"])
                done()
                await send_panel(bot, chat_id, channel_panel(gid, ch["id"]), "✅ Channel Added\n\n")
                return
            derived = derive_chat_from_link(value)
            if derived:
                ok, err = await check_bot_access(bot, derived)
                if ok:
                    ch = new_channel(gid, value, derived)
                    done()
                    await send_panel(bot, chat_id, channel_panel(gid, ch["id"]), "✅ Channel Added\n\n")
                    return
                sess["step"] = "chat"
                await bot.send_message(chat_id, "❌ " + err, reply_markup=cancel_markup(f"gc:{gid}"))
                return
            sess["step"] = "chat"
            await bot.send_message(
                chat_id,
                "Step 2/2: this is a private/invite link, so send the channel numeric ID (-100...) "
                "or @username.\n💡 You can also forward any post from that channel.\n"
                "Bot must be admin in the channel.", reply_markup=cancel_markup(f"gc:{gid}"))
        elif step == "chat":
            fo = getattr(m, "forward_origin", None)
            fch = (getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)) if fo else None
            chat = str(fch.id) if fch is not None else normalize_chat(value)
            if not chat:
                await m.reply_text("❌ Send a valid @username or numeric ID (-100...).")
                return
            ok, err = await check_bot_access(bot, chat)
            if not ok:
                await m.reply_text("❌ " + err)
                return
            ch = new_channel(gid, d["link"], chat)
            done()
            await send_panel(bot, chat_id, channel_panel(gid, ch["id"]), "✅ Channel Added\n\n")


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, u = update.effective_message, update.effective_user
    if not m or not u:
        return
    if OWNER_ID == 0:
        await m.reply_text("OWNER_ID is not set. Add it in the Render Environment and redeploy.")
        return
    if u.id != OWNER_ID:
        await m.reply_text("🔒 This is a private bot.")
        return
    text = m.text or ""
    sess = sessions.get(u.id)
    if sess and not text.startswith("/"):
        await handle_session(update, context, sess)
        return
    sessions.pop(u.id, None)
    t, markup = main_panel()
    await say(m, t, reply_markup=markup)


# ----------------------------------------------------------------- bot added / removed
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ev = update.my_chat_member
    if not ev or OWNER_ID == 0:
        return
    chat = ev.chat
    if chat.type == ChatType.PRIVATE:
        return
    old, new = ev.old_chat_member.status, ev.new_chat_member.status
    gone = ("left", "kicked")
    _bot_admin_cache.pop(chat.id, None)
    try:
        if old in gone and new not in gone:
            if chat.type == ChatType.CHANNEL:
                kb = InlineKeyboardMarkup([[btn("📢 Add as Required Channel", data=f"rch:{chat.id}", style="primary")]])
                what = "channel"
                ask = "Add it as a required channel?"
            else:
                if str(chat.id) in db.groups:
                    save_group(chat)
                    await context.bot.send_message(OWNER_ID, f"ℹ️ Bot is back in <b>{escape(chat.title)}</b> "
                                                   "(already in your list).", parse_mode="HTML")
                    return
                kb = InlineKeyboardMarkup([[btn("➕ Add to My Groups", data=f"add:{chat.id}", style="success")]])
                what = "group"
                ask = "Add it to your groups list?"
            await context.bot.send_message(
                OWNER_ID, f"➕ Bot was added to <b>{escape(chat.title)}</b> [{what}]\n<code>{chat.id}</code>\n\n{ask}\n"
                          "(Make the bot admin first.)", parse_mode="HTML", reply_markup=kb)
        elif new in gone and str(chat.id) in db.groups:
            await context.bot.send_message(
                OWNER_ID, f"⚠️ Bot was removed from <b>{escape(chat.title)}</b>. It is still in your list.",
                parse_mode="HTML")
    except Exception:
        log.exception("owner notification failed")


async def on_error(update, context):
    log.error("Update error", exc_info=context.error)


# ----------------------------------------------------------------- web server (Render keep-alive)
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def run_web():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Ping).serve_forever()


def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        sys.exit("BOT_TOKEN is not set (add it in Render Environment).")
    threading.Thread(target=run_web, daemon=True).start()

    asyncio.set_event_loop(asyncio.new_event_loop())   # Python 3.14 fix
    app = Application.builder().bot(StyledBot(token)).build()
    app.add_handler(CallbackQueryHandler(on_verify, pattern=r"^v:"))
    app.add_handler(CallbackQueryHandler(on_owner_button, pattern=r"^(m|g|gt|gm|ge|gd|gdy|gc|gca|gg|gra|gr|grd|gv|gvt|gvc|gvi|c|cd|cdy|col|add|rch|rchg):"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, on_private_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, gate_watch), group=1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    log.info("Bot starting... groups: %d", len(db.groups))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

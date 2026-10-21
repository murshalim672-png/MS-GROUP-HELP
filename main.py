# -*- coding: utf-8 -*-
"""Private group-help bot (owner only, button controlled).
 - Owner manages groups (add / change / delete) with buttons in the bot DM.
 - REQUIRED CHANNELS join gate (ported from the like-bot): JOIN CHANNEL 1, 2... + VERIFY.
 - Bot text style: first letter bold, rest small-caps (ported from the like-bot)."""
import asyncio, html, json, logging, os, re, sys, threading, time, uuid
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
DEFAULT_SETTINGS = {
    "verify_text": DEFAULT_VERIFY_TEXT,
    "verify_color": "success",
    "verify_icon": "",          # premium emoji id on the VERIFY button ("" = none)
    "channels": [],             # required channels: [{"id","link","chat"}]
    "delete_msg": True,         # delete the message of a user who has not joined
}


class Store:
    """JSON file: {"groups": {"<chat_id>": {title,type,added,gate}}, "settings": {...}}"""

    def __init__(self):
        self.data = {"groups": {}, "settings": json.loads(json.dumps(DEFAULT_SETTINGS))}
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self.data["groups"] = raw.get("groups", {})
                self.data["settings"].update(raw.get("settings", {}))
            except Exception:
                log.exception("could not read data.json")
        s = self.data["settings"]
        if s.get("verify_color") not in VALID_STYLES:
            s["verify_color"] = "success"

    @property
    def groups(self):
        return self.data["groups"]

    @property
    def settings(self):
        return self.data["settings"]

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
JOINED_CACHE_SECONDS = 60
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


# ----------------------------------------------------------------- REQUIRED CHANNELS (JOIN GATE)
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


async def missing_channels(bot, user_id):
    """Required channels this user has NOT joined yet (fresh check)."""
    if user_id == OWNER_ID:
        return []
    out = []
    for ch in db.settings["channels"]:
        if ch.get("chat") and not await is_member_of(bot, ch["chat"], user_id):
            out.append(ch)
    return out


def verify_button(user_id, use_icon=True):
    s = db.settings
    return btn(s["verify_text"], data=f"v:{user_id}", style=s["verify_color"],
               icon=(s.get("verify_icon") if use_icon else ""))


def join_markup(user_id, missing, use_icon=True):
    """Only the channels this user has NOT joined, numbered 1, 2, 3... + VERIFY (bound to the user)."""
    rows = [[btn(channel_button_name(n), url=ch["link"], style=CHANNEL_BUTTON_STYLE)]
            for n, ch in enumerate(missing, 1)]
    rows.append([verify_button(user_id, use_icon)])
    return InlineKeyboardMarkup(rows)


async def send_join_prompt(bot, chat_id, user, missing):
    text = (f"👋 Hello {name_text(user)}\n\n"
            "⚠️ You must join all required channels to use this group\n\n"
            "👇 Join the channels below, then tap Verify")
    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      reply_markup=join_markup(user.id, missing))
    except BadRequest as e:
        if not db.settings.get("verify_icon"):
            raise
        # Premium emoji icon refused by Telegram: fall back to a normal VERIFY button.
        log.warning("Join prompt failed with premium icon, retrying without it: %s", e)
        return await bot.send_message(chat_id, text, parse_mode="HTML",
                                      reply_markup=join_markup(user.id, missing, use_icon=False))


async def gate_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every message in the owner's groups."""
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not m or not user or user.is_bot or m.sender_chat or getattr(m, "is_automatic_forward", False):
        return
    g = db.groups.get(str(chat.id))
    if not g or not g.get("gate", True):
        return
    s = db.settings
    if not s["channels"] or user.id == OWNER_ID:
        return
    if time.time() - joined_cache.get(user.id, 0) < JOINED_CACHE_SECONDS:
        return
    if not await bot_is_admin(context.bot, chat.id):
        return
    missing = await missing_channels(context.bot, user.id)
    if not missing:
        joined_cache[user.id] = time.time()
        return
    try:     # group admins are never blocked
        cm = await context.bot.get_chat_member(chat.id, user.id)
        if cm.status in ("administrator", "creator"):
            return
    except Exception:
        pass
    if s["delete_msg"]:
        try:
            await m.delete()
        except Exception:
            pass
    old = prompts.get((chat.id, user.id))
    if old and time.time() - old[1] < PROMPT_SECONDS:
        return        # this user already has a join message waiting
    try:
        sent = await send_join_prompt(context.bot, chat.id, user, missing)
        prompts[(chat.id, user.id)] = (sent.message_id, time.time())
    except Exception:
        log.exception("could not send join prompt")


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
    joined_cache.pop(target, None)
    missing = await missing_channels(context.bot, target)
    if missing:
        try:
            try:
                await q.edit_message_reply_markup(reply_markup=join_markup(target, missing))
            except BadRequest as e:
                if "not modified" in str(e).lower() or not db.settings.get("verify_icon"):
                    raise
                await q.edit_message_reply_markup(reply_markup=join_markup(target, missing, use_icon=False))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("Verify refresh failed: %s", e)
        await q.answer("❌ You have not joined all required channels yet.", show_alert=True)
        return
    joined_cache[target] = time.time()
    prompts.pop((q.message.chat.id, target), None)
    await q.answer("✅ Verified successfully")
    try:
        await q.edit_message_text(
            f"👋 Hello {name_text(q.from_user)}\n\n🎉 Welcome to {escape(q.message.chat.title or 'the group')}\n\n"
            "❤️ Thank you for joining all the required channels\n\n"
            "✅ You are verified. Now you can use this group",
            parse_mode="HTML")
        later(_del_after(q.message, 10))
    except Exception:
        pass


# ----------------------------------------------------------------- group list helpers
def save_group(chat):
    gid = str(chat.id)
    old = db.groups.get(gid, {})
    new = gid not in db.groups
    db.groups[gid] = {"title": chat.title or gid, "type": chat.type,
                      "added": old.get("added", int(time.time())), "gate": old.get("gate", True)}
    db.save()
    return new


def replace_group(old_gid, chat):
    """Swap one group for another, keeping its position and gate setting."""
    keep_gate = db.groups.get(old_gid, {}).get("gate", True)
    new_gid = str(chat.id)
    items = list(db.groups.items())
    db.groups.clear()
    for k, v in items:
        if k == old_gid:
            db.groups[new_gid] = {"title": chat.title or new_gid, "type": chat.type,
                                  "added": v.get("added", int(time.time())), "gate": keep_gate}
        elif k != new_gid:
            db.groups[k] = v
    db.save()


def new_channel(link, chat):
    ch = {"id": uuid.uuid4().hex[:8], "link": link, "chat": chat}
    db.settings["channels"].append(ch)
    db.save()
    joined_cache.clear()
    return ch


def find_channel(cid):
    return next((c for c in db.settings["channels"] if c["id"] == cid), None)


def channel_number(cid):
    return next((i for i, c in enumerate(db.settings["channels"], 1) if c["id"] == cid), 0)


# ----------------------------------------------------------------- panels (owner, buttons only)
def onoff(v):
    return "ON ✅" if v else "OFF ⛔"


def main_panel():
    s = db.settings
    text = (
        "⚙️ <b>Control Panel</b>\n\n"
        f"📋 <b>Groups:</b> {len(db.groups)}\n"
        f"📢 <b>Required Channels:</b> {len(s['channels'])}\n"
        f"✅ <b>Verify Button</b> ({COLOR_LABELS[s['verify_color']]}): <code>{escape(s['verify_text'])}</code>\n"
        f"🗑 <b>Delete Unjoined User's Message:</b> {onoff(s['delete_msg'])}\n\n"
        "Choose what you want to do:")
    m = InlineKeyboardMarkup([
        [btn("📋 My Groups", data="m:groups", style="primary"), btn("➕ Add Group", data="m:addg", style="success")],
        [btn("📢 Required Channels", data="a:chs", style="primary")],
        [btn("✅ Verify Button Settings", data="a:v", style="success")],
        [btn(f"🗑 Delete Unjoined Msg: {onoff(s['delete_msg'])}", data="a:delmsg")],
        [btn("❌ Close", data="m:close", style="danger")],
    ])
    return text, m


async def groups_panel(bot):
    if db.groups:
        lines = []
        for i, (gid, g) in enumerate(db.groups.items(), 1):
            st = await bot_status(bot, int(gid))
            lines.append(f"<b>{i}.</b> {escape(g['title'])}\n    {st} · Gate {onoff(g.get('gate', True))}")
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
    n = len(db.settings["channels"])
    warn = "" if n else "\n⚠️ No required channels yet, so the gate does nothing. Add them in 📢 Required Channels."
    text = (f"📋 <b>{escape(g['title'])}</b>\n\n<code>{gid}</code>\n"
            f"Bot: {st}\n🔒 <b>Join Gate:</b> {onoff(g.get('gate', True))}\n"
            f"📢 <b>Required Channels:</b> {n}{warn}")
    m = InlineKeyboardMarkup([
        [btn(f"🔒 Join Gate: {onoff(g.get('gate', True))}", data=f"gt:{gid}", style="success" if g.get("gate", True) else "danger")],
        [btn("✏️ Change Group", data=f"ge:{gid}", style="primary"), btn("🗑 Delete", data=f"gd:{gid}", style="danger")],
        [btn("⬅️ Back", data="m:groups")],
    ])
    return text, m


def channels_panel():
    chans = db.settings["channels"]
    if chans:
        lines = "\n".join(f"{i}. {channel_button_name(i)} — <code>{escape(c['chat'] or '-')}</code>"
                          for i, c in enumerate(chans, 1))
    else:
        lines = "No channels added yet."
    text = ("📢 <b>Required Channels</b>\n"
            "Users must join all of these to use your groups.\n"
            "⚠️ The bot must be admin in every channel.\n"
            "🔒 Button name / colour are fixed. You can only add or delete.\n\n" + lines)
    rows = [[btn("📢 " + channel_button_name(i), data=f"c:{c['id']}", style=CHANNEL_BUTTON_STYLE)]
            for i, c in enumerate(chans, 1)]
    if len(chans) < MAX_CHANNELS:
        rows.append([btn("➕ Add Channel", data="a:chadd", style="success")])
    rows.append([btn("⬅️ Back", data="m:main")])
    return text, InlineKeyboardMarkup(rows)


def channel_panel(cid):
    ch = find_channel(cid)
    if not ch:
        return channels_panel()
    text = ("📢 <b>Channel Button</b>\n\n"
            f"Name: {channel_button_name(channel_number(cid))}\n"
            f"Link: <code>{escape(ch['link'])}</code>\n"
            f"Channel ID/Username: <code>{escape(ch['chat'] or 'Not Set')}</code>\n"
            f"Colour: {COLOR_LABELS[CHANNEL_BUTTON_STYLE]} (Fixed)")
    m = InlineKeyboardMarkup([[btn("🗑 Delete Channel", data=f"cd:{cid}", style="danger")],
                              [btn("⬅️ Back", data="a:chs")]])
    return text, m


def verify_panel():
    s = db.settings
    icon = s.get("verify_icon") or ""
    text = ("✅ <b>Verify Button</b>\n"
            "Shown under the channel buttons when a user has not joined.\n\n"
            f"Text: <code>{escape(s['verify_text'])}</code>\n"
            f"Colour: {COLOR_LABELS[s['verify_color']]}\n"
            f"💎 Premium Emoji: {'✅ Set' if icon else 'Not Set'}\n\n"
            "💡 To set a premium emoji: Edit Text, then send a message that has that emoji.")
    rows = [[btn("✏️ Edit Text", data="a:vtxt", style="success"), btn("🎨 Edit Colour", data="a:vcol", style="success")]]
    if icon:
        rows.append([btn("🗑 Remove Premium Emoji", data="a:vico", style="danger")])
    rows.append([btn("⬅️ Back", data="m:main")])
    return text, InlineKeyboardMarkup(rows)


def color_panel():
    m = InlineKeyboardMarkup([
        [btn("🟢 Green", data="col:verify:g", style="success"), btn("🔵 Blue", data="col:verify:b", style="primary"),
         btn("🔴 Red", data="col:verify:r", style="danger")],
        [btn("⬅️ Back", data="a:v")]])
    return "🎨 <b>Choose Button Colour:</b>", m


def cancel_markup():
    return InlineKeyboardMarkup([[btn("⬅️ Cancel", data="m:main")]])


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

    if kind == "add":        # from "bot was added" notification (group)
        try:
            chat = await bot.get_chat(int(arg))
            new = save_group(chat)
            await q.answer("Added" if new else "Already in list")
            await show(q, await group_panel(bot, str(chat.id)), "✅ Group Added\n\n" if new else "")
        except Exception as e:
            await q.answer(f"Failed: {e}", show_alert=True)
        return
    if kind == "rch":        # from notification (channel) -> required channel
        await q.answer()
        try:
            chat = await bot.get_chat(int(arg))
        except Exception as e:
            await bot.send_message(chat_id, f"❌ {escape(str(e))}", parse_mode="HTML")
            return
        ok, err = await check_bot_access(bot, str(chat.id))
        if not ok:
            await bot.send_message(chat_id, "❌ " + err)
            return
        link = f"https://t.me/{chat.username}" if chat.username else ""
        if not link:
            try:
                link = await bot.export_chat_invite_link(chat.id)
            except Exception:
                link = ""
        if link:
            ch = new_channel(link, str(chat.id))
            await send_panel(bot, chat_id, channel_panel(ch["id"]), "✅ Channel Added\n\n")
        else:
            sessions[uid] = {"action": "add", "step": "link", "data": {"chat": str(chat.id)}}
            await bot.send_message(chat_id, "This channel is private. Send its invite link (https://t.me/+...).",
                                   reply_markup=cancel_markup())
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

    if kind == "g":
        await show(q, await group_panel(bot, arg))
        return
    if kind == "gt":
        if arg in db.groups:
            db.groups[arg]["gate"] = not db.groups[arg].get("gate", True)
            db.save()
        await show(q, await group_panel(bot, arg))
        return
    if kind == "ge":
        if arg in db.groups:
            sessions[uid] = {"action": "editg", "gid": arg}
            await bot.send_message(chat_id, "✏️ Send the NEW group ID (-100...), @username or t.me link.",
                                   reply_markup=cancel_markup())
        return
    if kind == "gd":
        g = db.groups.get(arg)
        if g:
            m = InlineKeyboardMarkup([[btn("✅ Yes, Delete", data=f"gdy:{arg}", style="danger"),
                                       btn("⬅️ Cancel", data=f"g:{arg}")]])
            await show(q, (f"🗑 <b>Remove {escape(g['title'])} from your list?</b>", m))
        return
    if kind == "gdy":
        db.groups.pop(arg, None)
        db.save()
        await show(q, await groups_panel(bot), "🗑 Removed\n\n")
        return

    if kind == "a":
        if arg == "chs":
            await show(q, channels_panel())
        elif arg == "v":
            await show(q, verify_panel())
        elif arg == "vcol":
            await show(q, color_panel())
        elif arg == "vico":
            db.settings["verify_icon"] = ""
            db.save()
            await show(q, verify_panel(), "✅ Updated\n\n")
        elif arg == "delmsg":
            db.settings["delete_msg"] = not db.settings["delete_msg"]
            db.save()
            await show(q, main_panel())
        elif arg == "vtxt":
            sessions[uid] = {"action": "vtxt"}
            await bot.send_message(
                chat_id,
                "✏️ Send the new VERIFY button text.\n💎 You can also send a message with a premium emoji. "
                "It will be used on the button.", reply_markup=cancel_markup())
        elif arg == "chadd":
            if len(db.settings["channels"]) >= MAX_CHANNELS:
                await bot.send_message(chat_id, f"❌ Maximum {MAX_CHANNELS} channels allowed.")
                return
            sessions[uid] = {"action": "add", "step": "link", "data": {}}
            await bot.send_message(
                chat_id,
                "➕ <b>Add Channel</b>\n\nStep 1/2: send the channel join link (https://t.me/...).\n"
                "Button name and colour are automatic.\n"
                "💡 Or just add the bot to the channel as admin and tap the button I send you.",
                parse_mode="HTML", reply_markup=cancel_markup())
        return

    if kind == "c":
        await show(q, channel_panel(arg))
        return
    if kind == "cd":
        m = InlineKeyboardMarkup([[btn("✅ Yes, Delete", data=f"cdy:{arg}", style="danger"),
                                   btn("⬅️ Cancel", data=f"c:{arg}")]])
        await show(q, ("🗑 <b>Delete this channel button?</b>", m))
        return
    if kind == "cdy":
        db.settings["channels"] = [c for c in db.settings["channels"] if c["id"] != arg]
        db.save()
        joined_cache.clear()
        await show(q, channels_panel(), "🗑 Deleted\n\n")
        return
    if kind == "col" and arg == "verify":
        style = COLOR_CODES.get(arg2)
        if style:
            db.settings["verify_color"] = style
            db.save()
            await show(q, verify_panel(), "✅ Colour Updated\n\n")


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

    if action == "vtxt":
        icon = ""
        value, icon = extract_custom_emoji(m)
        if not value or len(value) > 64:
            await m.reply_text("❌ Button text must be between 1 and 64 characters.")
            return
        db.settings["verify_text"] = value
        db.settings["verify_icon"] = icon
        db.save()
        done()
        await send_panel(bot, chat_id, verify_panel(), "✅ Updated\n\n")
        return

    if action == "add":
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
                ch = new_channel(value, d["chat"])
                done()
                await send_panel(bot, chat_id, channel_panel(ch["id"]), "✅ Channel Added\n\n")
                return
            derived = derive_chat_from_link(value)
            if derived:
                ok, err = await check_bot_access(bot, derived)
                if ok:
                    ch = new_channel(value, derived)
                    done()
                    await send_panel(bot, chat_id, channel_panel(ch["id"]), "✅ Channel Added\n\n")
                    return
                sess["step"] = "chat"
                await bot.send_message(chat_id, "❌ " + err, reply_markup=cancel_markup())
                return
            sess["step"] = "chat"
            await bot.send_message(
                chat_id,
                "Step 2/2: this is a private/invite link, so send the channel numeric ID (-100...) "
                "or @username.\n💡 You can also forward any post from that channel.\n"
                "Bot must be admin in the channel.", reply_markup=cancel_markup())
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
            ch = new_channel(d["link"], chat)
            done()
            await send_panel(bot, chat_id, channel_panel(ch["id"]), "✅ Channel Added\n\n")


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
    app.add_handler(CallbackQueryHandler(on_owner_button, pattern=r"^(m|g|gt|ge|gd|gdy|a|c|cd|cdy|col|add|rch)(:|$)"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, on_private_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, gate_watch), group=1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    log.info("Bot starting... groups: %d, required channels: %d", len(db.groups), len(db.settings["channels"]))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

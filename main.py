# -*- coding: utf-8 -*-
"""GroupHelp Bot - Telegram group management bot (Rose-style) with a
configurable "Wrong Format" numeric guard."""
import asyncio, copy, functools, html, json, logging, os, re, sys, threading, time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import ChatPermissions, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("grouphelp")

OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
GROUPS = (ChatType.GROUP, ChatType.SUPERGROUP)
ANON_ADMIN_BOT = 1087968824

# ----------------------------------------------------------------- storage
DEFAULTS = {
    "fmt": {
        "on": False,                # feature OFF until you turn it on
        "msg": "❌ Wrong Format",
        "cmds": [],                 # command lines shown under the message
        "min": 7, "max": 12,
        "mode": "pure",             # pure = message is only a number, contains = number anywhere
        "exempt_admins": True,
        "delete_msg": True,
        "autodel": 15,              # seconds, 0 = keep the warning
    },
    "welcome": {"on": True, "text": "Welcome {mention} to {chat}! 🎉"},
    "goodbye": {"on": False, "text": "Goodbye {first}!"},
    "rules": "",
    "warn_limit": 3,
    "warns": {},
    "notes": {},
    "filters": [],
    "blacklist": [],
    "locks": [],
    "flood": 0,
}


def merge(base, doc):
    out = copy.deepcopy(base)
    for k, v in doc.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


class Store:
    def __init__(self):
        self.cache = {}
        self.uri = os.getenv("MONGO_URI", "").strip()
        self.col = None
        self.path = "data.json"
        self.file = {}
        if not self.uri and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self.file = json.load(fh)
            except Exception:
                log.exception("data.json read failed")

    def _col(self):
        if self.col is None:
            from motor.motor_asyncio import AsyncIOMotorClient
            self.col = AsyncIOMotorClient(self.uri)[os.getenv("DB_NAME", "grouphelp")]["chats"]
        return self.col

    async def get(self, chat_id):
        if chat_id in self.cache:
            return self.cache[chat_id]
        raw = {}
        try:
            if self.uri:
                raw = await self._col().find_one({"_id": chat_id}) or {}
                raw.pop("_id", None)
            else:
                raw = self.file.get(str(chat_id), {})
        except Exception:
            log.exception("db read failed")
        self.cache[chat_id] = merge(DEFAULTS, raw)
        return self.cache[chat_id]

    async def save(self, chat_id):
        doc = self.cache.get(chat_id)
        if doc is None:
            return
        try:
            if self.uri:
                await self._col().replace_one({"_id": chat_id}, {**doc, "_id": chat_id}, upsert=True)
            else:
                self.file[str(chat_id)] = doc
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.file, fh, ensure_ascii=False)
                os.replace(tmp, self.path)
        except Exception:
            log.exception("db write failed")


store = Store()

# ----------------------------------------------------------------- helpers
_tasks = set()


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


def esc(s):
    return html.escape(str(s or ""))


def mention(u):
    name = esc(u.first_name or u.username or str(u.id))
    return f'<a href="tg://user?id={u.id}">{name}</a>'


def rest(m):
    parts = (m.text or m.caption or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def render(tpl, user, chat, **extra):
    s = esc(tpl)
    rep = {"{mention}": mention(user), "{first}": esc(user.first_name), "{id}": str(user.id),
           "{username}": esc("@" + user.username) if user.username else esc(user.first_name),
           "{chat}": esc(chat.title)}
    rep.update(extra)
    for k, v in rep.items():
        s = s.replace(k, v)
    return s


def parse_dur(s):
    mt = re.fullmatch(r"(\d+)([smhd])", (s or "").lower())
    if not mt:
        return None
    return int(mt[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[mt[2]]


async def is_admin(context, chat_id, user_id, msg=None):
    if user_id == OWNER_ID or user_id == ANON_ADMIN_BOT:
        return True
    if msg is not None and msg.sender_chat and msg.sender_chat.id == chat_id:
        return True
    try:
        cm = await context.bot.get_chat_member(chat_id, user_id)
        return cm.status in ("administrator", "creator")
    except Exception:
        return False


def admin_cmd(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat, m, u = update.effective_chat, update.effective_message, update.effective_user
        if not m or not u:
            return
        if chat.type not in GROUPS:
            await m.reply_text("ये कमांड सिर्फ ग्रुप में चलती है।")
            return
        if not await is_admin(context, chat.id, u.id, m):
            await m.reply_text("❌ ये कमांड सिर्फ एडमिन के लिए है।")
            return
        return await func(update, context)
    return wrapper


def group_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in GROUPS:
            await update.effective_message.reply_text("ये कमांड सिर्फ ग्रुप में चलती है।")
            return
        return await func(update, context)
    return wrapper


async def get_target(update, context):
    m = update.effective_message
    args = list(context.args or [])
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        return u.id, u, args
    if args and args[0].isdigit():
        uid = int(args[0])
        try:
            u = (await context.bot.get_chat_member(m.chat_id, uid)).user
        except Exception:
            u = None
        return uid, u, args[1:]
    return None, None, args


def who(uid, u):
    return mention(u) if u else f"<code>{uid}</code>"


async def prep(update, context, usage, protect=True):
    m = update.effective_message
    uid, u, args = await get_target(update, context)
    if uid is None:
        await m.reply_text(usage)
        return None
    if protect:
        if uid == context.bot.id:
            await m.reply_text("मैं खुद पर ये नहीं कर सकता 🙂")
            return None
        if await is_admin(context, m.chat_id, uid):
            await m.reply_text("❌ एडमिन पर ये नहीं चलेगा।")
            return None
    return uid, u, args


def say(m, text, **kw):
    return m.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, **kw)


# ----------------------------------------------------------------- basics
HELP = """<b>🤖 GroupHelp Bot</b>

<b>Basic</b>
/id /ping /rules /notes /get &lt;name&gt; /whois

<b>Admin – Moderation</b>
/ban /unban /kick /mute /unmute (reply ya user-id)
/mute 10m ya /mute &lt;id&gt; 2h (s/m/h/d)
/warn /unwarn /warns /resetwarns /setwarnlimit &lt;n&gt;
/del (reply) /purge (reply) /pin /unpin /admins

<b>Welcome / Rules</b>
/setwelcome &lt;text&gt; /welcome on|off
/setgoodbye &lt;text&gt; /goodbye on|off
/setrules &lt;text&gt; /clearrules
Placeholders: {mention} {first} {id} {username} {chat}

<b>Notes / Filters / Blacklist</b>
/save &lt;name&gt; &lt;text&gt; /clear &lt;name&gt; (use: #name)
/filter &lt;word&gt; &lt;reply&gt; /stop &lt;word&gt; /filters
/addblacklist &lt;word&gt; /unblacklist &lt;word&gt; /blacklist

<b>Locks / Flood</b>
/lock &lt;type&gt; /unlock &lt;type&gt; /locks
types: link sticker photo video document voice gif forward contact poll
/setflood &lt;n&gt; (0 = off)

<b>🔢 Wrong Format Guard (number blocker)</b>
/setformat on|off
/setwrong &lt;message&gt;
/addcmd &lt;line&gt; /delcmd &lt;no&gt; /setcmds &lt;lines&gt; /clearcmds /cmds
/setrange &lt;min&gt; &lt;max&gt;  (default 7 12)
/formatmode pure|contains
/formatadmin on|off (admins ko chhoot)
/formatdelete on|off /formatautodel &lt;sec&gt;
/formatstatus"""


async def start(update, context):
    m = update.effective_message
    await say(m, "👋 मैं एक ग्रुप मैनेजमेंट बॉट हूँ। मुझे ग्रुप में <b>एडमिन</b> बनाओ (Delete messages, Ban users का अधिकार) और /help देखो।")


async def help_cmd(update, context):
    await say(update.effective_message, HELP)


async def ping(update, context):
    t = time.time()
    msg = await update.effective_message.reply_text("🏓")
    await msg.edit_text(f"🏓 Pong! {int((time.time() - t) * 1000)} ms")


async def id_cmd(update, context):
    m, chat, u = update.effective_message, update.effective_chat, update.effective_user
    t = f"👤 Your ID: <code>{u.id}</code>\n💬 Chat ID: <code>{chat.id}</code>"
    if m.reply_to_message and m.reply_to_message.from_user:
        t += f"\n↩️ Replied user ID: <code>{m.reply_to_message.from_user.id}</code>"
    await say(m, t)


async def whois(update, context):
    m = update.effective_message
    uid, u, _ = await get_target(update, context)
    if uid is None:
        u = update.effective_user
        uid = u.id
    t = f"👤 {who(uid, u)}\n🆔 <code>{uid}</code>"
    if u and u.username:
        t += f"\n🔗 @{esc(u.username)}"
    if update.effective_chat.type in GROUPS:
        t += f"\n⭐ Admin: {'Yes' if await is_admin(context, m.chat_id, uid) else 'No'}"
    await say(m, t)


@group_only
async def admins(update, context):
    m = update.effective_message
    lst = await context.bot.get_chat_administrators(m.chat_id)
    lines = [f"• {mention(a.user)}{' 👑' if a.status == 'creator' else ''}" for a in lst if not a.user.is_bot]
    await say(m, "<b>Admins</b>\n" + "\n".join(lines))


# ----------------------------------------------------------------- moderation
async def _fail(m, e):
    await m.reply_text(f"❌ नहीं हो पाया: {e}\n(बॉट को एडमिन + ज़रूरी अधिकार दो)")


@admin_cmd
async def ban(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /ban (reply ya user-id)")
    if not r:
        return
    uid, u, _ = r
    try:
        await context.bot.ban_chat_member(m.chat_id, uid)
        await say(m, f"🔨 Banned {who(uid, u)}")
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def unban(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /unban (reply ya user-id)", protect=False)
    if not r:
        return
    uid, u, _ = r
    try:
        await context.bot.unban_chat_member(m.chat_id, uid, only_if_banned=True)
        await say(m, f"✅ Unbanned {who(uid, u)}")
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def kick(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /kick (reply ya user-id)")
    if not r:
        return
    uid, u, _ = r
    try:
        await context.bot.ban_chat_member(m.chat_id, uid)
        await context.bot.unban_chat_member(m.chat_id, uid, only_if_banned=True)
        await say(m, f"👢 Kicked {who(uid, u)}")
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def mute(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /mute [10m|2h|1d] (reply ya user-id)")
    if not r:
        return
    uid, u, args = r
    secs = parse_dur(args[0]) if args else None
    until = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None
    try:
        await context.bot.restrict_chat_member(m.chat_id, uid, ChatPermissions(can_send_messages=False),
                                               until_date=until)
        await say(m, f"🔇 Muted {who(uid, u)}" + (f" for {args[0]}" if secs else ""))
    except Exception as e:
        await _fail(m, e)


FULL = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                       can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                       can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                       can_add_web_page_previews=True, can_invite_users=True)


@admin_cmd
async def unmute(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /unmute (reply ya user-id)", protect=False)
    if not r:
        return
    uid, u, _ = r
    try:
        await context.bot.restrict_chat_member(m.chat_id, uid, FULL)
        await say(m, f"🔊 Unmuted {who(uid, u)}")
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def warn(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /warn [reason] (reply ya user-id)")
    if not r:
        return
    uid, u, args = r
    doc = await store.get(m.chat_id)
    n = doc["warns"].get(str(uid), 0) + 1
    lim = doc["warn_limit"]
    reason = " ".join(args)
    if n >= lim:
        doc["warns"].pop(str(uid), None)
        await store.save(m.chat_id)
        try:
            await context.bot.ban_chat_member(m.chat_id, uid)
            await say(m, f"🚫 {who(uid, u)} को {lim}/{lim} warns मिले — Banned.")
        except Exception as e:
            await _fail(m, e)
        return
    doc["warns"][str(uid)] = n
    await store.save(m.chat_id)
    await say(m, f"⚠️ {who(uid, u)} warned ({n}/{lim})" + (f"\nReason: {esc(reason)}" if reason else ""))


@admin_cmd
async def unwarn(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /unwarn (reply ya user-id)", protect=False)
    if not r:
        return
    uid, u, _ = r
    doc = await store.get(m.chat_id)
    n = doc["warns"].get(str(uid), 0)
    if n <= 1:
        doc["warns"].pop(str(uid), None)
    else:
        doc["warns"][str(uid)] = n - 1
    await store.save(m.chat_id)
    await say(m, f"✅ {who(uid, u)} का एक warn हटाया।")


@group_only
async def warns_cmd(update, context):
    m = update.effective_message
    uid, u, _ = await get_target(update, context)
    if uid is None:
        u = update.effective_user
        uid = u.id
    doc = await store.get(m.chat_id)
    await say(m, f"⚠️ {who(uid, u)}: {doc['warns'].get(str(uid), 0)}/{doc['warn_limit']} warns")


@admin_cmd
async def resetwarns(update, context):
    m = update.effective_message
    r = await prep(update, context, "Usage: /resetwarns (reply ya user-id)", protect=False)
    if not r:
        return
    uid, u, _ = r
    doc = await store.get(m.chat_id)
    doc["warns"].pop(str(uid), None)
    await store.save(m.chat_id)
    await say(m, f"✅ {who(uid, u)} के सारे warns reset।")


@admin_cmd
async def setwarnlimit(update, context):
    m = update.effective_message
    a = context.args
    if not a or not a[0].isdigit() or not (1 <= int(a[0]) <= 20):
        await m.reply_text("Usage: /setwarnlimit 3  (1-20)")
        return
    doc = await store.get(m.chat_id)
    doc["warn_limit"] = int(a[0])
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Warn limit = {a[0]}")


@admin_cmd
async def del_cmd(update, context):
    m = update.effective_message
    if not m.reply_to_message:
        await m.reply_text("जिस मैसेज को हटाना है उस पर reply करके /del लिखो।")
        return
    try:
        await m.reply_to_message.delete()
        await m.delete()
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def purge(update, context):
    m = update.effective_message
    if not m.reply_to_message:
        await m.reply_text("जहाँ से हटाना शुरू करना है उस मैसेज पर reply करके /purge लिखो।")
        return
    ids = list(range(m.reply_to_message.message_id, m.message_id + 1))
    try:
        for i in range(0, len(ids), 100):
            await context.bot.delete_messages(m.chat_id, ids[i:i + 100])
        note = await context.bot.send_message(m.chat_id, f"🧹 Purged {len(ids)} messages.")
        later(_del_after(note, 5))
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def pin(update, context):
    m = update.effective_message
    if not m.reply_to_message:
        await m.reply_text("जिसे pin करना है उस पर reply करो।")
        return
    try:
        await m.reply_to_message.pin()
    except Exception as e:
        await _fail(m, e)


@admin_cmd
async def unpin(update, context):
    m = update.effective_message
    try:
        if m.reply_to_message:
            await context.bot.unpin_chat_message(m.chat_id, m.reply_to_message.message_id)
        else:
            await context.bot.unpin_chat_message(m.chat_id)
        await m.reply_text("📌 Unpinned.")
    except Exception as e:
        await _fail(m, e)


# ----------------------------------------------------------------- welcome / rules
def _toggle_arg(args):
    if args and args[0].lower() in ("on", "yes", "true", "1"):
        return True
    if args and args[0].lower() in ("off", "no", "false", "0"):
        return False
    return None


def make_text_setter(key, label):
    @admin_cmd
    async def setter(update, context):
        m = update.effective_message
        txt = rest(m) or (m.reply_to_message.text if m.reply_to_message and m.reply_to_message.text else "")
        if not txt:
            await m.reply_text(f"Usage: /set{key} <text>\nPlaceholders: {{mention}} {{first}} {{id}} {{username}} {{chat}}")
            return
        doc = await store.get(m.chat_id)
        doc[key]["text"] = txt
        await store.save(m.chat_id)
        await m.reply_text(f"✅ {label} message set.")
    return setter


def make_toggle(key, label):
    @admin_cmd
    async def toggle(update, context):
        m = update.effective_message
        v = _toggle_arg(context.args)
        if v is None:
            await m.reply_text(f"Usage: /{key} on|off")
            return
        doc = await store.get(m.chat_id)
        doc[key]["on"] = v
        await store.save(m.chat_id)
        await m.reply_text(f"✅ {label} {'ON' if v else 'OFF'}")
    return toggle


setwelcome = make_text_setter("welcome", "Welcome")
setgoodbye = make_text_setter("goodbye", "Goodbye")
welcome_toggle = make_toggle("welcome", "Welcome")
goodbye_toggle = make_toggle("goodbye", "Goodbye")


async def on_join(update, context):
    m, chat = update.effective_message, update.effective_chat
    doc = await store.get(chat.id)
    if not doc["welcome"]["on"]:
        return
    for u in m.new_chat_members:
        if u.is_bot:
            continue
        try:
            await say(m, render(doc["welcome"]["text"], u, chat))
        except Exception:
            pass


async def on_leave(update, context):
    m, chat = update.effective_message, update.effective_chat
    doc = await store.get(chat.id)
    u = m.left_chat_member
    if doc["goodbye"]["on"] and u and not u.is_bot:
        try:
            await say(m, render(doc["goodbye"]["text"], u, chat))
        except Exception:
            pass


@group_only
async def rules(update, context):
    m = update.effective_message
    doc = await store.get(m.chat_id)
    await say(m, "📜 <b>Rules</b>\n" + esc(doc["rules"]) if doc["rules"] else "अभी कोई rules सेट नहीं हैं।")


@admin_cmd
async def setrules(update, context):
    m = update.effective_message
    txt = rest(m)
    if not txt:
        await m.reply_text("Usage: /setrules <text>")
        return
    doc = await store.get(m.chat_id)
    doc["rules"] = txt
    await store.save(m.chat_id)
    await m.reply_text("✅ Rules saved.")


@admin_cmd
async def clearrules(update, context):
    doc = await store.get(update.effective_chat.id)
    doc["rules"] = ""
    await store.save(update.effective_chat.id)
    await update.effective_message.reply_text("✅ Rules cleared.")


# ----------------------------------------------------------------- notes / filters / blacklist
NOTE_RE = re.compile(r"^[\w-]{1,32}$")


def payload(m, skip_words=1):
    parts = (m.text or "").split(None, skip_words + 0)
    text = parts[skip_words] if len(parts) > skip_words else ""
    if not text and m.reply_to_message:
        text = m.reply_to_message.text or m.reply_to_message.caption or ""
    return text


@admin_cmd
async def save(update, context):
    m = update.effective_message
    if not context.args or not NOTE_RE.match(context.args[0]):
        await m.reply_text("Usage: /save <name> <text>  (ya reply karke /save <name>)")
        return
    parts = (m.text or "").split(None, 2)
    text = parts[2] if len(parts) > 2 else (
        (m.reply_to_message.text or m.reply_to_message.caption or "") if m.reply_to_message else "")
    if not text:
        await m.reply_text("Note ka text भी दो।")
        return
    doc = await store.get(m.chat_id)
    doc["notes"][context.args[0].lower()] = text
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Saved. Use: #{context.args[0].lower()} ya /get {context.args[0].lower()}")


@group_only
async def get_note(update, context):
    m = update.effective_message
    if not context.args:
        await m.reply_text("Usage: /get <name>")
        return
    doc = await store.get(m.chat_id)
    t = doc["notes"].get(context.args[0].lower())
    await (say(m, esc(t)) if t else m.reply_text("ऐसा कोई note नहीं।"))


@group_only
async def notes_cmd(update, context):
    doc = await store.get(update.effective_chat.id)
    n = sorted(doc["notes"])
    await say(update.effective_message,
              "<b>Notes</b>\n" + "\n".join(f"• #{esc(x)}" for x in n) if n else "कोई notes नहीं हैं।")


@admin_cmd
async def clear_note(update, context):
    m = update.effective_message
    doc = await store.get(m.chat_id)
    if context.args and doc["notes"].pop(context.args[0].lower(), None) is not None:
        await store.save(m.chat_id)
        await m.reply_text("✅ Note deleted.")
    else:
        await m.reply_text("Usage: /clear <name>  (note मिला नहीं)")


@admin_cmd
async def filter_cmd(update, context):
    m = update.effective_message
    parts = (m.text or "").split(None, 2)
    if len(parts) < 2:
        await m.reply_text("Usage: /filter <word> <reply text>")
        return
    reply = parts[2] if len(parts) > 2 else (
        (m.reply_to_message.text or "") if m.reply_to_message else "")
    if not reply:
        await m.reply_text("Reply text भी दो।")
        return
    kw = parts[1].lower()
    doc = await store.get(m.chat_id)
    doc["filters"] = [f for f in doc["filters"] if f["k"] != kw] + [{"k": kw, "v": reply}]
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Filter set: {kw}")


@admin_cmd
async def stop_filter(update, context):
    m = update.effective_message
    if not context.args:
        await m.reply_text("Usage: /stop <word>")
        return
    doc = await store.get(m.chat_id)
    kw = context.args[0].lower()
    before = len(doc["filters"])
    doc["filters"] = [f for f in doc["filters"] if f["k"] != kw]
    await store.save(m.chat_id)
    await m.reply_text("✅ Removed." if len(doc["filters"]) < before else "ऐसा filter नहीं मिला।")


@group_only
async def filters_cmd(update, context):
    doc = await store.get(update.effective_chat.id)
    f = [x["k"] for x in doc["filters"]]
    await say(update.effective_message,
              "<b>Filters</b>\n" + "\n".join(f"• {esc(x)}" for x in f) if f else "कोई filters नहीं हैं।")


@admin_cmd
async def addbl(update, context):
    m = update.effective_message
    w = rest(m).lower()
    if not w:
        await m.reply_text("Usage: /addblacklist <word>")
        return
    doc = await store.get(m.chat_id)
    if w not in doc["blacklist"]:
        doc["blacklist"].append(w)
        await store.save(m.chat_id)
    await m.reply_text(f"✅ Blacklisted: {w}")


@admin_cmd
async def rmbl(update, context):
    m = update.effective_message
    w = rest(m).lower()
    doc = await store.get(m.chat_id)
    if w in doc["blacklist"]:
        doc["blacklist"].remove(w)
        await store.save(m.chat_id)
        await m.reply_text("✅ Removed.")
    else:
        await m.reply_text("Usage: /unblacklist <word>  (word मिला नहीं)")


@group_only
async def bl_list(update, context):
    doc = await store.get(update.effective_chat.id)
    await say(update.effective_message,
              "<b>Blacklist</b>\n" + "\n".join(f"• {esc(x)}" for x in doc["blacklist"])
              if doc["blacklist"] else "Blacklist खाली है।")


# ----------------------------------------------------------------- locks / flood
LOCKS = ["link", "sticker", "photo", "video", "document", "voice", "gif", "forward", "contact", "poll"]
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.I)


def lock_hit(m, locks):
    ents = list(m.entities or []) + list(m.caption_entities or [])
    txt = m.text or m.caption or ""
    checks = {
        "link": lambda: any(e.type in ("url", "text_link") for e in ents) or bool(LINK_RE.search(txt)),
        "sticker": lambda: bool(m.sticker),
        "photo": lambda: bool(m.photo),
        "video": lambda: bool(m.video or m.video_note),
        "document": lambda: bool(m.document and not m.animation),
        "voice": lambda: bool(m.voice or m.audio),
        "gif": lambda: bool(m.animation),
        "forward": lambda: bool(m.forward_origin),
        "contact": lambda: bool(m.contact),
        "poll": lambda: bool(m.poll),
    }
    for name in locks:
        fn = checks.get(name)
        if fn and fn():
            return name
    return None


def make_lock(on):
    @admin_cmd
    async def lock(update, context):
        m = update.effective_message
        t = (context.args[0].lower() if context.args else "")
        if t not in LOCKS:
            await m.reply_text("Types: " + " ".join(LOCKS))
            return
        doc = await store.get(m.chat_id)
        if on and t not in doc["locks"]:
            doc["locks"].append(t)
        if not on and t in doc["locks"]:
            doc["locks"].remove(t)
        await store.save(m.chat_id)
        await m.reply_text(f"{'🔒 Locked' if on else '🔓 Unlocked'}: {t}")
    return lock


lock_cmd, unlock_cmd = make_lock(True), make_lock(False)


@group_only
async def locks_cmd(update, context):
    doc = await store.get(update.effective_chat.id)
    await update.effective_message.reply_text(
        "🔒 Locked: " + (", ".join(doc["locks"]) if doc["locks"] else "कुछ नहीं"))


@admin_cmd
async def setflood(update, context):
    m = update.effective_message
    a = context.args
    if not a or not a[0].isdigit():
        await m.reply_text("Usage: /setflood 6  (5 सेकंड में 6 मैसेज पर mute; 0 = off)")
        return
    doc = await store.get(m.chat_id)
    doc["flood"] = int(a[0])
    await store.save(m.chat_id)
    await m.reply_text("✅ Antiflood " + (f"= {a[0]} msgs / 5s" if int(a[0]) else "OFF"))


flood_log = defaultdict(lambda: deque(maxlen=50))

# ----------------------------------------------------------------- WRONG FORMAT GUARD
SEP_RE = re.compile(r"[\s\-\.,_+()/\u200b\u200c\u200d]")
RUN_RE = re.compile(r"\d(?:[\d\s\-\.,_()]*\d)?")


def bad_number(text, f):
    t = text.strip()
    if not t:
        return False
    lo, hi = f["min"], f["max"]
    if f["mode"] == "pure":
        s = SEP_RE.sub("", t)
        return bool(s) and s.isdecimal() and lo <= len(s) <= hi
    for run in RUN_RE.findall(t):
        n = sum(1 for c in run if c.isdecimal())
        if lo <= n <= hi:
            return True
    return False


last_fmt = {}


async def punish_format(update, context, doc):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    f = doc["fmt"]
    if f["delete_msg"]:
        try:
            await m.delete()
        except Exception:
            pass
    key = (chat.id, user.id)
    now = time.time()
    if now - last_fmt.get(key, 0) < 8:       # anti-spam: no repeated replies
        return
    last_fmt[key] = now
    cmds = "\n".join(esc(c) for c in f["cmds"])
    msg = render(f["msg"], user, chat, **{"{commands}": cmds})
    if "{commands}" not in f["msg"] and cmds:
        msg += "\n\n" + cmds
    try:
        sent = await context.bot.send_message(chat.id, msg, parse_mode=ParseMode.HTML,
                                              disable_web_page_preview=True)
        if f["autodel"] > 0:
            later(_del_after(sent, f["autodel"]))
    except Exception:
        log.exception("format reply failed")


@admin_cmd
async def setformat(update, context):
    m = update.effective_message
    v = _toggle_arg(context.args)
    if v is None:
        await m.reply_text("Usage: /setformat on|off")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["on"] = v
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Wrong Format guard {'ON ✅' if v else 'OFF ⛔'}")


@admin_cmd
async def setwrong(update, context):
    m = update.effective_message
    txt = rest(m) or (m.reply_to_message.text if m.reply_to_message and m.reply_to_message.text else "")
    if not txt:
        await m.reply_text("Usage: /setwrong <message>\nPlaceholders: {mention} {first} {id} {username} {chat} {commands}\n"
                           "{commands} नहीं लिखोगे तो commands अपने आप नीचे जुड़ जाएंगे।")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["msg"] = txt
    await store.save(m.chat_id)
    await m.reply_text("✅ Wrong Format message set.")


@admin_cmd
async def addcmd(update, context):
    m = update.effective_message
    line = rest(m)
    if not line:
        await m.reply_text("Usage: /addcmd /like region uid - free likes")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["cmds"].append(line)
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Added (#{len(doc['fmt']['cmds'])})")


@admin_cmd
async def delcmd(update, context):
    m = update.effective_message
    doc = await store.get(m.chat_id)
    a = context.args
    if not a or not a[0].isdigit() or not (1 <= int(a[0]) <= len(doc["fmt"]["cmds"])):
        await m.reply_text("Usage: /delcmd <number>  (नंबर /cmds से देखो)")
        return
    doc["fmt"]["cmds"].pop(int(a[0]) - 1)
    await store.save(m.chat_id)
    await m.reply_text("✅ Removed.")


@admin_cmd
async def setcmds(update, context):
    m = update.effective_message
    lines = [x.strip() for x in rest(m).splitlines() if x.strip()]
    if not lines:
        await m.reply_text("Usage:\n/setcmds /like region uid - free likes\n/info uid - player info\n(हर लाइन एक command)")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["cmds"] = lines
    await store.save(m.chat_id)
    await m.reply_text(f"✅ {len(lines)} commands set.")


@admin_cmd
async def clearcmds(update, context):
    doc = await store.get(update.effective_chat.id)
    doc["fmt"]["cmds"] = []
    await store.save(update.effective_chat.id)
    await update.effective_message.reply_text("✅ Commands list cleared.")


@admin_cmd
async def cmds_cmd(update, context):
    doc = await store.get(update.effective_chat.id)
    c = doc["fmt"]["cmds"]
    await update.effective_message.reply_text(
        "\n".join(f"{i}. {x}" for i, x in enumerate(c, 1)) if c else "कोई command सेट नहीं है। /addcmd या /setcmds से जोड़ो।")


@admin_cmd
async def setrange(update, context):
    m = update.effective_message
    a = context.args
    if len(a) != 2 or not all(x.isdigit() for x in a) or not (1 <= int(a[0]) <= int(a[1]) <= 40):
        await m.reply_text("Usage: /setrange 7 12")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["min"], doc["fmt"]["max"] = int(a[0]), int(a[1])
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Range: {a[0]}–{a[1]} digits")


@admin_cmd
async def formatmode(update, context):
    m = update.effective_message
    if not context.args or context.args[0].lower() not in ("pure", "contains"):
        await m.reply_text("Usage: /formatmode pure|contains\npure = सिर्फ नंबर वाला मैसेज\ncontains = मैसेज में कहीं भी नंबर")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["mode"] = context.args[0].lower()
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Mode: {context.args[0].lower()}")


def make_fmt_flag(cmd, key, label):
    @admin_cmd
    async def flag(update, context):
        m = update.effective_message
        v = _toggle_arg(context.args)
        if v is None:
            await m.reply_text(f"Usage: /{cmd} on|off")
            return
        doc = await store.get(m.chat_id)
        doc["fmt"][key] = v
        await store.save(m.chat_id)
        await m.reply_text(f"✅ {label}: {'ON' if v else 'OFF'}")
    return flag


formatadmin = make_fmt_flag("formatadmin", "exempt_admins", "Admins exempt")
formatdelete = make_fmt_flag("formatdelete", "delete_msg", "Delete user's number message")


@admin_cmd
async def formatautodel(update, context):
    m = update.effective_message
    a = context.args
    if not a or not a[0].isdigit():
        await m.reply_text("Usage: /formatautodel 15  (सेकंड, 0 = मैसेज रहने दो)")
        return
    doc = await store.get(m.chat_id)
    doc["fmt"]["autodel"] = int(a[0])
    await store.save(m.chat_id)
    await m.reply_text(f"✅ Auto-delete: {a[0]}s")


@admin_cmd
async def formatstatus(update, context):
    doc = await store.get(update.effective_chat.id)
    f = doc["fmt"]
    t = (f"<b>🔢 Wrong Format Guard</b>\n"
         f"Status: {'ON ✅' if f['on'] else 'OFF ⛔'}\n"
         f"Range: {f['min']}–{f['max']} digits\nMode: {f['mode']}\n"
         f"Admins exempt: {f['exempt_admins']}\nDelete number msg: {f['delete_msg']}\n"
         f"Auto-delete reply: {f['autodel']}s\n\n"
         f"<b>Message:</b>\n{esc(f['msg'])}\n\n<b>Commands ({len(f['cmds'])}):</b>\n"
         + ("\n".join(esc(c) for c in f["cmds"]) or "—"))
    await say(update.effective_message, t)


# ----------------------------------------------------------------- main watcher
async def watcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not m or not user or user.is_bot or chat.type not in GROUPS:
        return
    doc = await store.get(chat.id)
    text = m.text or m.caption or ""

    async def admin():
        return await is_admin(context, chat.id, user.id, m)

    # 1) Wrong Format guard
    f = doc["fmt"]
    if f["on"] and text and bad_number(text, f):
        if not (f["exempt_admins"] and await admin()):
            await punish_format(update, context, doc)
            return

    # 2) locks
    hit = lock_hit(m, doc["locks"]) if doc["locks"] else None
    if hit and not await admin():
        try:
            await m.delete()
        except Exception:
            pass
        return

    # 3) blacklist
    if text and doc["blacklist"]:
        low = text.lower()
        if any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", low) for w in doc["blacklist"]):
            if not await admin():
                try:
                    await m.delete()
                except Exception:
                    pass
                return

    # 4) antiflood
    if doc["flood"] > 0 and not m.edit_date:
        dq = flood_log[(chat.id, user.id)]
        now = time.time()
        dq.append(now)
        recent = [t for t in dq if now - t <= 5]
        if len(recent) >= doc["flood"] and not await admin():
            dq.clear()
            try:
                await context.bot.restrict_chat_member(
                    chat.id, user.id, ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc) + timedelta(minutes=5))
                await say(m, f"🌊 Flood! {mention(user)} 5 मिनट के लिए mute।")
            except Exception:
                pass
            return

    if m.edit_date or not text:
        return

    # 5) #notes
    if text.startswith("#"):
        name = text[1:].split()[0].lower() if len(text) > 1 else ""
        if name in doc["notes"]:
            await say(m, esc(doc["notes"][name]))
            return

    # 6) filters
    low = text.lower()
    for fl in doc["filters"]:
        if re.search(r"(?<!\w)" + re.escape(fl["k"]) + r"(?!\w)", low):
            await say(m, render(fl["v"], user, chat))
            break


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
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


# ----------------------------------------------------------------- run
def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        sys.exit("BOT_TOKEN set nahi hai! (.env ya Render Environment me daalo)")
    threading.Thread(target=run_web, daemon=True).start()
    log.info("Storage: %s", "MongoDB" if store.uri else "local data.json")

    asyncio.set_event_loop(asyncio.new_event_loop())   # Python 3.14 fix
    app = Application.builder().token(token).build()
    C = lambda name, fn: app.add_handler(CommandHandler(name, fn))
    for name, fn in [
        ("start", start), ("help", help_cmd), ("ping", ping), ("id", id_cmd), ("whois", whois),
        ("admins", admins), ("ban", ban), ("unban", unban), ("kick", kick), ("mute", mute),
        ("unmute", unmute), ("warn", warn), ("unwarn", unwarn), ("warns", warns_cmd),
        ("resetwarns", resetwarns), ("setwarnlimit", setwarnlimit), ("del", del_cmd),
        ("purge", purge), ("pin", pin), ("unpin", unpin),
        ("setwelcome", setwelcome), ("welcome", welcome_toggle),
        ("setgoodbye", setgoodbye), ("goodbye", goodbye_toggle),
        ("rules", rules), ("setrules", setrules), ("clearrules", clearrules),
        ("save", save), ("get", get_note), ("notes", notes_cmd), ("clear", clear_note),
        ("filter", filter_cmd), ("stop", stop_filter), ("filters", filters_cmd),
        ("addblacklist", addbl), ("unblacklist", rmbl), ("blacklist", bl_list),
        ("lock", lock_cmd), ("unlock", unlock_cmd), ("locks", locks_cmd), ("setflood", setflood),
        ("setformat", setformat), ("setwrong", setwrong), ("addcmd", addcmd), ("delcmd", delcmd),
        ("setcmds", setcmds), ("clearcmds", clearcmds), ("cmds", cmds_cmd),
        ("setrange", setrange), ("formatmode", formatmode), ("formatadmin", formatadmin),
        ("formatdelete", formatdelete), ("formatautodel", formatautodel),
        ("formatstatus", formatstatus),
    ]:
        C(name, fn)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_leave))
    app.add_handler(MessageHandler(~filters.COMMAND & ~filters.StatusUpdate.ALL, watcher), group=1)
    app.add_error_handler(on_error)
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

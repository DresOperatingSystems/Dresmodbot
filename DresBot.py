import logging
import re
import requests
import json
import os
from typing import Set, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
from inspect import signature

from telegram import Update, ChatPermissions, ChatMember, ChatMemberOwner, ChatMemberAdministrator
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Configuration
TOKEN = "place your bot token here"  # rotate in production
BOT_ADMINS: Set[int] = {place your account user id here}
DDG_API_URL = "https://api.duckduckgo.com/"
STORE_FILE = "welcome_store.json"

BLACKLIST: Set[int] = set()

# Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Persistent store helpers
def load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_FILE):
        return {"welcomes": {}, "warns": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load store: %s", e)
        return {"welcomes": {}, "warns": {}}

def save_store(store: Dict[str, Any]) -> None:
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save store: %s", e)

STORE = load_store()

# Privacy-aware HTTP session
FAKE_IP = "203.0.113.42"
def _make_privacy_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "DNT": "1",
        "User-Agent": "DuckBot/1.0",
        "X-Forwarded-For": FAKE_IP,
    })

    class NoCookieRedirectAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            request.headers.pop("Cookie", None)
            response = super().send(request, **kwargs)
            response.headers.pop("Set-Cookie", None)
            response.headers.pop("Set-Cookie2", None)
            return response

    retry = requests.packages.urllib3.util.retry.Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = NoCookieRedirectAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

_privacy_session = _make_privacy_session()

# DuckDuckGo helper
IP_QUERY_RE = re.compile(r"\b(?:ip|my|your|the|your own|user's?)\s*ip\s*(?:address)?\b", re.IGNORECASE)
def is_ip_query(text: Optional[str]) -> bool:
    return bool(text and IP_QUERY_RE.search(text))

def duckduckgo_search(query: str) -> str:
    params = {"q": query, "format": "json", "no_redirect": 1, "skip_disambig": 1}
    try:
        resp = _privacy_session.get(DDG_API_URL, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("DuckDuckGo request failed: %s", e)
        return "Sorry, I couldn't reach DuckDuckGo."

    if data.get("Answer"):
        return data["Answer"]
    if data.get("AbstractText"):
        return data["AbstractText"]
    topics = data.get("RelatedTopics")
    if topics:
        titles = [t["Text"] for t in topics if "Text" in t][:3]
        return " | ".join(titles) or "No results found."
    return "No results found."

# ChatPermissions compatibility
CANONICAL_PERMS = {
    "can_send_messages": False,
    "can_send_media_messages": False,
    "can_send_polls": False,
    "can_send_other_messages": False,
    "can_add_web_page_previews": False,
    "can_change_info": False,
    "can_invite_users": False,
    "can_pin_messages": False,
}

def make_chat_permissions_from_dict(perms: Dict[str, bool]) -> ChatPermissions:
    accepted = {}
    try:
        sig = signature(ChatPermissions)
        valid_params = set(sig.parameters.keys())
        for k, v in perms.items():
            if k in valid_params:
                accepted[k] = v
    except Exception:
        # fallback try direct construction
        try:
            return ChatPermissions(**perms)
        except TypeError:
            # map media -> other if needed
            alt_map = {"can_send_media_messages": "can_send_other_messages"}
            alt = {}
            for k, v in perms.items():
                if k in alt_map and alt_map[k] not in perms:
                    alt[alt_map[k]] = v
                else:
                    alt[k] = v
            try:
                return ChatPermissions(**alt)
            except Exception:
                return ChatPermissions()
    try:
        return ChatPermissions(**accepted)
    except Exception:
        # final fallback
        return ChatPermissions()

def build_full_mute_permissions() -> ChatPermissions:
    return make_chat_permissions_from_dict(CANONICAL_PERMS.copy())

# Utilities
def parse_duration(s: Optional[str]) -> Optional[timedelta]:
    if not s:
        return None
    s0 = s.strip().lower()
    m = re.match(r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?$", s0)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if not unit:
        return timedelta(seconds=val)
    if unit.startswith("s"):
        return timedelta(seconds=val)
    if unit.startswith("m"):
        return timedelta(minutes=val)
    if unit.startswith("h"):
        return timedelta(hours=val)
    if unit.startswith("d"):
        return timedelta(days=val)
    return timedelta(seconds=val)

# Admin helpers
def is_bot_owner(user_id: int) -> bool:
    return user_id in BOT_ADMINS

async def is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

async def check_issuer_permission(update: Update, context: ContextTypes.DEFAULT_TYPE, need: str) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        await update.effective_message.reply_text("Unable to determine user/chat.")
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        await update.effective_message.reply_text("Failed to fetch your chat permissions.")
        return False

    if member.status == "creator":
        return True
    if member.status != "administrator":
        await update.effective_message.reply_text("You must be a chat admin to use this command.")
        return False

    can_restrict = bool(getattr(member, "can_restrict_members", False))
    can_delete = bool(getattr(member, "can_delete_messages", False))

    if need in ("ban", "kick", "mute") and not can_restrict:
        await update.effective_message.reply_text("You don't have permission to restrict/ban members (can_restrict_members).")
        return False
    if need == "delete" and not can_delete:
        await update.effective_message.reply_text("You don't have permission to delete messages (can_delete_messages).")
        return False
    return True

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Hi! I am a DuckDuckGo web search and group moderation bot created by DresOS.")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id in BLACKLIST:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /search <query>")
        return
    query = " ".join(context.args)
    if is_ip_query(query):
        await update.effective_message.reply_text("I’m not allowed to share IP address information.")
        return
    ans = duckduckgo_search(query)
    await update.effective_message.reply_text(ans)

async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    requester = update.effective_user.id
    if not is_bot_owner(requester):
        await update.effective_message.reply_text("You are not authorized to use this command.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /blacklist <user_id>")
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("User id must be integer.")
        return
    BLACKLIST.add(uid)
    await update.effective_message.reply_text(f"User {uid} blacklisted.")

async def unblacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    requester = update.effective_user.id
    if not is_bot_owner(requester):
        await update.effective_message.reply_text("You are not authorized to use this command.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unblacklist <user_id>")
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text("User id must be integer.")
        return
    BLACKLIST.discard(uid)
    await update.effective_message.reply_text(f"User {uid} removed from blacklist.")

async def list_blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    requester = update.effective_user.id
    if not is_bot_owner(requester):
        await update.effective_message.reply_text("You are not authorized to use this command.")
        return
    if BLACKLIST:
        await update.effective_message.reply_text("Blacklist: " + ", ".join(map(str, BLACKLIST)))
    else:
        await update.effective_message.reply_text("Blacklist empty.")

# Kick
async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "kick"):
        return
    chat = update.effective_chat
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide numeric user id or reply.")
            return
    else:
        await update.effective_message.reply_text("Usage: /kick <user_id or reply>")
        return
    try:
        await context.bot.ban_chat_member(chat.id, target)
        await context.bot.unban_chat_member(chat.id, target)
        await update.effective_message.reply_text(f"Kicked {target}.")
    except Exception as e:
        logger.exception("kick failed")
        await update.effective_message.reply_text(f"Failed to kick: {e}")

# Ban/unban
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "ban"):
        return
    chat = update.effective_chat
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide numeric user id or reply.")
            return
    else:
        await update.effective_message.reply_text("Usage: /ban <user_id or reply> [duration]")
        return

    until = None
    if len(context.args) >= 2:
        dur = parse_duration(context.args[1])
        if dur:
            until = datetime.now(timezone.utc) + dur
    try:
        await context.bot.ban_chat_member(chat.id, target, until_date=until)
        await update.effective_message.reply_text(f"Banned {target}.")
    except Exception as e:
        logger.exception("ban failed")
        await update.effective_message.reply_text(f"Failed to ban: {e}")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "ban"):
        return
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide numeric user id or reply.")
            return
    else:
        await update.effective_message.reply_text("Usage: /unban <user_id or reply>")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target)
        await update.effective_message.reply_text(f"Unbanned {target}.")
    except Exception as e:
        logger.exception("unban failed")
        await update.effective_message.reply_text(f"Failed to unban: {e}")

# Mute/unmute
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "mute"):
        return
    chat = update.effective_chat
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
        dur_arg = context.args[0] if context.args else None
    elif context.args:
        if len(context.args) == 1:
            await update.effective_message.reply_text("Reply to a user's message and use /mute <duration>, or use /mute <user_id> <duration>.")
            return
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide a numeric user id or reply.")
            return
        dur_arg = context.args[1]
    else:
        await update.effective_message.reply_text("Usage: /mute <duration> (reply) OR /mute <user_id> <duration>\nExamples: 10m, 2h")
        return

    dur = parse_duration(dur_arg)
    if not dur:
        await update.effective_message.reply_text("Invalid duration. Examples: 10m, 2h")
        return
    until = datetime.now(timezone.utc) + dur
    perms = build_full_mute_permissions()
    try:
        await context.bot.restrict_chat_member(chat.id, target, permissions=perms, until_date=until)
        await update.effective_message.reply_text(f"Muted {target} for {dur_arg}.")
    except Exception as e:
        logger.exception("mute failed")
        await update.effective_message.reply_text(f"Failed to mute: {e}")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "mute"):
        return
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide numeric user id or reply.")
            return
    else:
        await update.effective_message.reply_text("Usage: /unmute <user_id or reply>")
        return
    default_perms = {
        "can_send_messages": True,
        "can_send_media_messages": True,
        "can_send_polls": True,
        "can_send_other_messages": True,
        "can_add_web_page_previews": True,
        "can_change_info": False,
        "can_invite_users": True,
        "can_pin_messages": False,
    }
    try:
        perms = make_chat_permissions_from_dict(default_perms)
        await context.bot.restrict_chat_member(update.effective_chat.id, target, permissions=perms)
        await update.effective_message.reply_text(f"Unmuted {target}.")
    except Exception as e:
        logger.exception("unmute failed")
        await update.effective_message.reply_text(f"Failed to unmute: {e}")

# Warn
async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_issuer_permission(update, context, "kick"):
        return
    chat = update.effective_chat
    if update.effective_message.reply_to_message:
        target = update.effective_message.reply_to_message.from_user.id
    elif context.args:
        try:
            target = int(context.args[0])
        except Exception:
            await update.effective_message.reply_text("Provide numeric user id or reply.")
            return
    else:
        await update.effective_message.reply_text("Usage: /warn <user_id or reply>")
        return

    chat_key = str(chat.id)
    STORE.setdefault("warns", {})
    STORE["warns"].setdefault(chat_key, {})
    count = STORE["warns"][chat_key].get(str(target), 0) + 1
    STORE["warns"][chat_key][str(target)] = count
    save_store(STORE)
    await update.effective_message.reply_text(f"Warned {target} ({count} total).")

# Welcome config
async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_chat_admin(context, update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("You must be a chat admin to set welcome message.")
        return
    if not context.args and not update.effective_message.reply_to_message:
        await update.effective_message.reply_text("Usage: /setwelcome <text> [--channel <link>]. Use {user_mention} in text.")
        return
    args = context.args.copy()
    channel_link = None
    if "--channel" in args:
        i = args.index("--channel")
        if i + 1 < len(args):
            channel_link = args[i + 1]
            del args[i:i+2]
    text = " ".join(args).strip()
    if not text and update.effective_message.reply_to_message and update.effective_message.reply_to_message.text:
        text = update.effective_message.reply_to_message.text.strip()
    if not text:
        await update.effective_message.reply_text("No welcome text provided.")
        return
    chat_key = str(update.effective_chat.id)
    STORE.setdefault("welcomes", {})
    STORE["welcomes"][chat_key] = {"message": text, "channel_link": channel_link, "last_welcome_message_id": None}
    save_store(STORE)
    await update.effective_message.reply_text("Welcome message saved for this chat.")

async def clearwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_chat_admin(context, update.effective_chat.id, update.effective_user.id):
        await update.effective_message.reply_text("You must be a chat admin to clear welcome.")
        return
    chat_key = str(update.effective_chat.id)
    if "welcomes" in STORE and chat_key in STORE["welcomes"]:
        del STORE["welcomes"][chat_key]
        save_store(STORE)
        await update.effective_message.reply_text("Welcome cleared.")
    else:
        await update.effective_message.reply_text("No welcome configured.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Commands:\n"
        "/search <query>\n"
        "/kick <user_id or reply>\n"
        "/ban <user_id or reply> [duration]\n"
        "/unban <user_id or reply>\n"
        "/mute <duration> (reply) OR /mute <user_id> <duration>\n"
        "/unmute <user_id or reply>\n"
        "/warn <user_id or reply>\n"
        "/setwelcome <text> [--channel <link>] (use {user_mention})\n"
        "/clearwelcome\n"
    )
    await update.effective_message.reply_text(help_text)

# Welcome handler
async def welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Ensure update.message exists and contains new_chat_members
    if not update.message:
        logger.debug("welcome_handler: no update.message")
        return
    new_members = getattr(update.message, "new_chat_members", None)
    if not new_members:
        logger.debug("welcome_handler: no new_chat_members")
        return

    chat = update.effective_chat
    if not chat:
        logger.debug("welcome_handler: no effective_chat")
        return

    chat_key = str(chat.id)
    if "welcomes" not in STORE or chat_key not in STORE["welcomes"]:
        logger.debug("welcome_handler: no welcome config for chat %s", chat_key)
        return

    cfg = STORE["welcomes"][chat_key]
    template = cfg.get("message", "")
    channel = cfg.get("channel_link")
    last_msg_id = cfg.get("last_welcome_message_id")

    for member in new_members:
        # Build mention safely
        parse_mode = None
        mention = None
        try:
            mention = member.mention_html()
            parse_mode = "HTML"
        except Exception:
            mention = getattr(member, "full_name", str(member.id))
            parse_mode = None

        text = template.replace("{user_mention}", mention)
        if channel:
            if not text.endswith("\n"):
                text += "\n"
            text += f"\nChannel: {channel}"

        # Try to delete the previous welcome message (if we have its id)
        if last_msg_id:
            try:
                await context.bot.delete_message(chat.id, last_msg_id)
            except Exception as e:
                logger.debug("Failed to delete previous welcome message (%s): %s", last_msg_id, e)

        # Send the welcome message
        try:
            logger.info("Sending welcome message in chat %s for user %s", chat.id, member.id)
            sent = await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=parse_mode, disable_web_page_preview=False)
            # store id
            STORE["welcomes"][chat_key]["last_welcome_message_id"] = sent.message_id
            save_store(STORE)
        except Exception as e:
            logger.exception("Failed to send welcome message in chat %s: %s", chat.id, e)

# Misc
async def block_ip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message and update.effective_message.text and is_ip_query(update.effective_message.text):
        await update.effective_message.reply_text("I’m not allowed to share IP address information.")

async def ignore_non_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return

# Main
def main() -> None:
    if not TOKEN:
        raise RuntimeError("TOKEN not set")
    app = ApplicationBuilder().token(TOKEN).build()

    # core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search_cmd))

    # blacklist
    app.add_handler(CommandHandler("blacklist", blacklist_cmd))
    app.add_handler(CommandHandler("unblacklist", unblacklist_cmd))
    app.add_handler(CommandHandler("list_blacklist", list_blacklist_cmd))

    # moderation
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))

    # welcome config
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("clearwelcome", clearwelcome_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # NEW_CHAT_MEMBERS handler
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_handler))

    # block ip queries first
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, block_ip_handler), 1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ignore_non_commands), 2)

    logger.info("Bot starting")
    app.run_polling()

if __name__ == "__main__":
    main()

# code by DresOS
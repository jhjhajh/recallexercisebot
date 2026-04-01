"""
recall_exercise_bot — Recall Exercise Agent for Telegram
Requirements: pip install "python-telegram-bot==20.7" apscheduler python-dotenv
"""

import json
import logging
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["BOT_TOKEN"]
SUPER_ADMIN_ID    = str(os.environ["SUPER_ADMIN_ID"])
REMINDER_INTERVAL = int(os.environ.get("REMINDER_INTERVAL", 15))

DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
MEMBERS_FILE  = os.path.join(DATA_DIR, "members.json")
ADMINS_FILE   = os.path.join(DATA_DIR, "admins.json")
DIRECTORY_FILE = os.path.join(DATA_DIR, "directory.json")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Directory I/O ─────────────────────────────────────────────────────────────

def load_directory() -> dict:
    if not os.path.exists(DIRECTORY_FILE):
        return {}
    with open(DIRECTORY_FILE) as f:
        return json.load(f)

def save_directory(directory: dict):
    with open(DIRECTORY_FILE, "w") as f:
        json.dump(directory, f, indent=2)

def update_directory(user):
    if user is None or user.is_bot:
        return
    directory = load_directory()
    directory[str(user.id)] = {
        "name":     user.full_name,
        "username": f"@{user.username}" if user.username else None
    }
    save_directory(directory)

# ── Admin list I/O ────────────────────────────────────────────────────────────

def load_admins() -> list[dict]:
    if not os.path.exists(ADMINS_FILE):
        save_admins([])
        return []
    with open(ADMINS_FILE) as f:
        return json.load(f)

def save_admins(admins: list[dict]):
    with open(ADMINS_FILE, "w") as f:
        json.dump(admins, f, indent=2)

def is_admin(user_id: str) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    return any(str(a["user_id"]) == user_id for a in load_admins())

# ── Member list I/O ───────────────────────────────────────────────────────────

def load_members() -> list[dict]:
    if not os.path.exists(MEMBERS_FILE):
        save_members([])
        return []
    with open(MEMBERS_FILE) as f:
        return json.load(f)

def save_members(members: list[dict]):
    with open(MEMBERS_FILE, "w") as f:
        json.dump(members, f, indent=2)

# ── Session state (in-memory) ─────────────────────────────────────────────────
session = {
    "active":       False,
    "chat_id":      None,
    "officer_id":   None,
    "officer_name": None,
    "t0":           None,
    "responses":    {},
    "reminder_job": None,
}

def reset_session():
    session.update({
        "active":       False,
        "chat_id":      None,
        "officer_id":   None,
        "officer_name": None,
        "t0":           None,
        "responses":    {},
        "reminder_job": None,
    })

def pending_members() -> list[dict]:
    members   = load_members()
    responded = set(session["responses"].keys())
    return [m for m in members if str(m["user_id"]) not in responded]

def fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"

def member_tags(members: list[dict]) -> str:
    """Build clickable @mention tags for a list of members."""
    return " ".join(
        f"[{m['name']}](tg://user?id={m['user_id']})" for m in members
    )

def build_report(closed: datetime) -> str:
    members   = load_members()
    total     = len(members)
    responded = session["responses"]
    t0        = session["t0"]
    duration  = (closed - t0).total_seconds()

    lines = [
        "📋 *RECALL EXERCISE SUMMARY*",
        f"Date: {t0.strftime('%d/%m/%Y')}",
        f"Initiated: {t0.strftime('%H:%M')} UTC by {session['officer_name']}",
        f"Closed: {closed.strftime('%H:%M')} UTC  |  Total Duration: {fmt_duration(duration)}",
        f"Full Strength Achieved: {len(responded)}/{total} members",
        "",
        "*Response Log:*",
    ]
    for i, (uid, data) in enumerate(
        sorted(responded.items(), key=lambda x: x[1]["ts"]), 1
    ):
        elapsed = fmt_duration((data["ts"] - t0).total_seconds())
        lines.append(
            f"{i}. {data['name']} — {data['ts'].strftime('%H:%M')} UTC — {elapsed} after initiation"
        )

    if len(responded) < total:
        pending = [m["name"] for m in members if str(m["user_id"]) not in responded]
        lines += ["", "*❌ Did not respond:*"] + [f"• {n}" for n in pending]

    return "\n".join(lines)

# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="UTC")

async def send_reminder(bot: Bot):
    if not session["active"]:
        return
    pending = pending_members()
    if not pending:
        return
    tags = member_tags(pending)
    await bot.send_message(
        chat_id=session["chat_id"],
        text=(
            f"⏰ *RECALL REMINDER*\n"
            f"The following members have *not yet responded*:\n{tags}\n\n"
            f"Please reply *ACK* or send ✅ to confirm you are active."
        ),
        parse_mode="Markdown"
    )

# ── Recall commands ───────────────────────────────────────────────────────────

async def cmd_recall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if session["active"]:
        await update.message.reply_text(
            "⚠️ A recall exercise is already active. Use /endrecall first."
        )
        return

    members = load_members()
    if not members:
        await update.message.reply_text(
            "❌ No members loaded. Ask an admin to add members with /addmember."
        )
        return

    now     = datetime.now(timezone.utc)
    officer = update.effective_user
    session.update({
        "active":       True,
        "chat_id":      update.effective_chat.id,
        "officer_id":   str(officer.id),
        "officer_name": officer.full_name,
        "t0":           now,
        "responses":    {},
    })

    tags = member_tags(members)

    # Send the main recall message with everyone tagged
    await update.message.reply_text(
        "🚨 *RECALL EXERCISE INITIATED*\n\n"
        f"Initiated by: {officer.full_name}\n"
        f"Time: {now.strftime('%H:%M')} UTC\n\n"
        f"The following *{len(members)}* members are required to respond:\n"
        f"{tags}\n\n"
        f"Please reply *ACK* or send ✅ to confirm you are active.",
        parse_mode="Markdown"
    )

    job = scheduler.add_job(
        send_reminder,
        trigger="interval",
        minutes=REMINDER_INTERVAL,
        args=[ctx.bot],
        id="recall_reminder",
        replace_existing=True,
    )
    session["reminder_job"] = job
    log.info("Recall started by %s in chat %s", officer.full_name, update.effective_chat.id)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not session["active"]:
        await update.message.reply_text("ℹ️ No recall exercise is currently active.")
        return

    members = load_members()
    total   = len(members)
    done    = len(session["responses"])
    pending = pending_members()

    lines = [f"📊 *RECALL STATUS* — {done}/{total} responded\n"]
    if pending:
        lines.append("*⏳ Pending:*")
        lines += [f"• {m['name']}" for m in pending]
    else:
        lines.append("✅ All members have responded!")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not session["active"]:
        await update.message.reply_text("ℹ️ No recall exercise is currently active.")
        return
    await send_reminder(ctx.bot)


async def cmd_endrecall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not session["active"]:
        await update.message.reply_text("ℹ️ No recall exercise is currently active.")
        return

    if session["reminder_job"]:
        try:
            session["reminder_job"].remove()
        except Exception:
            pass

    closed = datetime.now(timezone.utc)
    report = build_report(closed)
    await update.message.reply_text(report, parse_mode="Markdown")
    log.info("Recall ended. Report posted to chat %s", session["chat_id"])
    reset_session()

# ── Response tracking ─────────────────────────────────────────────────────────

async def track_response(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    update_directory(update.effective_user)

    if not session["active"]:
        return

    user = update.effective_user
    if user is None or user.is_bot:
        return

    members = load_members()
    uid_str = str(user.id)
    member  = next((m for m in members if str(m["user_id"]) == uid_str), None)
    if not member:
        return

    if uid_str in session["responses"]:
        return

    msg          = (update.message.text or "").strip().upper()
    ack_keywords = {"ACK", "✅", "👍", "ACKNOWLEDGED", "PRESENT"}
    if not any(k in msg for k in ack_keywords):
        return

    now = datetime.now(timezone.utc)
    session["responses"][uid_str] = {"name": member["name"], "ts": now}
    elapsed = fmt_duration((now - session["t0"]).total_seconds())

    await update.message.reply_text(
        f"✅ *{member['name']}* acknowledged! ({elapsed} after initiation)",
        parse_mode="Markdown"
    )
    log.info("%s acknowledged at %s", member["name"], now)

    if len(session["responses"]) == len(members):
        await ctx.bot.send_message(
            chat_id=session["chat_id"],
            text=(
                f"🎉 *Full Strength Achieved!*\n"
                f"All *{len(members)}* members have acknowledged.\n"
                f"Use /endrecall to generate the final report."
            ),
            parse_mode="Markdown"
        )

# ── Member management commands ────────────────────────────────────────────────

async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Bulk load members on first boot. Only works if members.json is empty.
    Only super admin can run this.

    Usage — send as one message:
    /setup
    John Doe, 111111111
    Jane Smith, 222222222
    """
    if str(update.effective_user.id) != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Only the super admin can run /setup.")
        return

    existing = load_members()
    if existing:
        await update.message.reply_text(
            f"⛔ Setup already done — {len(existing)} members already loaded.\n"
            "Use /addmember or /removemember to make changes."
        )
        return

    text  = update.message.text or ""
    lines = [l.strip() for l in text.split("\n")[1:] if l.strip()]

    if not lines:
        await update.message.reply_text(
            "⚠️ No members provided. Send it like this:\n\n"
            "`/setup`\n"
            "`John Doe, 111111111`\n"
            "`Jane Smith, 222222222`",
            parse_mode="Markdown"
        )
        return

    members = []
    errors  = []
    for i, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            errors.append(f"Line {i}: `{line}` — expected `Name, user_id`")
            continue
        members.append({"name": parts[0], "user_id": parts[1]})

    if errors:
        await update.message.reply_text(
            "❌ Some lines had errors:\n" + "\n".join(errors) +
            "\n\nFix and resend. Nothing was saved.",
            parse_mode="Markdown"
        )
        return

    save_members(members)
    lines_out = [f"📋 *Setup complete — {len(members)} members loaded:*\n"]
    for i, m in enumerate(members, 1):
        lines_out.append(f"{i}. {m['name']} — `{m['user_id']}`")
    await update.message.reply_text("\n".join(lines_out), parse_mode="Markdown")


async def cmd_addmember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ You are not authorised to manage members.")
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: `/addmember <user_id> <Full Name>`\n"
            "Example: `/addmember 112233445 John Doe`\n\n"
            "💡 Members can send /myid to get their Telegram user ID.",
            parse_mode="Markdown"
        )
        return

    uid     = str(ctx.args[0])
    name    = " ".join(ctx.args[1:])
    members = load_members()

    if any(str(m["user_id"]) == uid for m in members):
        await update.message.reply_text(
            f"ℹ️ User ID `{uid}` is already in the member list.", parse_mode="Markdown"
        )
        return

    members.append({"name": name, "user_id": uid})
    save_members(members)
    await update.message.reply_text(
        f"✅ *{name}* (ID: `{uid}`) added. Total members: {len(members)}.",
        parse_mode="Markdown"
    )


async def cmd_removemember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ You are not authorised to manage members.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Usage: `/removemember <user_id or Full Name>`", parse_mode="Markdown"
        )
        return

    query   = " ".join(ctx.args)
    members = load_members()
    uid_str = query.strip()
    by_id   = [m for m in members if str(m["user_id"]) == uid_str]

    if by_id:
        new_list = [m for m in members if str(m["user_id"]) != uid_str]
        removed  = by_id
    else:
        q        = query.lower()
        new_list = [m for m in members if m["name"].lower() != q]
        removed  = [m for m in members if m["name"].lower() == q]

    if not removed:
        await update.message.reply_text(
            f"❌ No member found matching `{query}`.\nUse /listmembers to see the full list.",
            parse_mode="Markdown"
        )
        return

    save_members(new_list)
    names = ", ".join(m["name"] for m in removed)
    await update.message.reply_text(
        f"🗑️ Removed: *{names}*. Remaining members: {len(new_list)}.",
        parse_mode="Markdown"
    )


async def cmd_renamemember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ You are not authorised to manage members.")
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: `/renamemember <user_id> <New Name>`", parse_mode="Markdown"
        )
        return

    uid      = str(ctx.args[0])
    new_name = " ".join(ctx.args[1:])
    members  = load_members()
    member   = next((m for m in members if str(m["user_id"]) == uid), None)

    if not member:
        await update.message.reply_text(f"❌ No member with ID `{uid}` found.", parse_mode="Markdown")
        return

    old_name       = member["name"]
    member["name"] = new_name
    save_members(members)
    await update.message.reply_text(
        f"✏️ Renamed *{old_name}* → *{new_name}*.", parse_mode="Markdown"
    )


async def cmd_listmembers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    members = load_members()
    if not members:
        await update.message.reply_text("📋 Member list is empty. Use /addmember to add members.")
        return

    lines = [f"📋 *Member List* ({len(members)} total)\n"]
    for i, m in enumerate(members, 1):
        lines.append(f"{i}. {m['name']} — `{m['user_id']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Admin management commands ─────────────────────────────────────────────────

async def cmd_addadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ Only admins can add other admins.")
        return

    if len(ctx.args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: `/addadmin <user_id> <Full Name>`\n"
            "Example: `/addadmin 112233445 Jane Smith`",
            parse_mode="Markdown"
        )
        return

    uid    = str(ctx.args[0])
    name   = " ".join(ctx.args[1:])
    admins = load_admins()

    if uid == SUPER_ADMIN_ID or any(str(a["user_id"]) == uid for a in admins):
        await update.message.reply_text(
            f"ℹ️ User ID `{uid}` is already an admin.", parse_mode="Markdown"
        )
        return

    admins.append({"name": name, "user_id": uid})
    save_admins(admins)
    await update.message.reply_text(
        f"✅ *{name}* (ID: `{uid}`) is now an admin. Total admins: {len(admins) + 1}.",
        parse_mode="Markdown"
    )


async def cmd_removeadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ Only admins can remove other admins.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Usage: `/removeadmin <user_id or Full Name>`", parse_mode="Markdown"
        )
        return

    query   = " ".join(ctx.args)
    admins  = load_admins()
    uid_str = query.strip()

    if uid_str == SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ The super admin cannot be removed.")
        return

    by_id = [a for a in admins if str(a["user_id"]) == uid_str]
    if by_id:
        new_list = [a for a in admins if str(a["user_id"]) != uid_str]
        removed  = by_id
    else:
        q        = query.lower()
        new_list = [a for a in admins if a["name"].lower() != q]
        removed  = [a for a in admins if a["name"].lower() == q]

    if not removed:
        await update.message.reply_text(
            f"❌ No admin found matching `{query}`.\nUse /listadmins to see the list.",
            parse_mode="Markdown"
        )
        return

    save_admins(new_list)
    names = ", ".join(a["name"] for a in removed)
    await update.message.reply_text(
        f"🗑️ Removed admin: *{names}*. Remaining admins: {len(new_list) + 1}.",
        parse_mode="Markdown"
    )


async def cmd_listadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(str(update.effective_user.id)):
        await update.message.reply_text("⛔ Only admins can view the admin list.")
        return

    admins = load_admins()
    lines  = [f"🔑 *Admin List* ({len(admins) + 1} total)\n"]
    lines.append(f"1. Super Admin — `{SUPER_ADMIN_ID}` 👑")
    for i, a in enumerate(admins, 2):
        lines.append(f"{i}. {a['name']} — `{a['user_id']}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Utility ───────────────────────────────────────────────────────────────────

async def cmd_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Only the super admin can use /lookup.")
        return

    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Usage: `/lookup @username` or `/lookup Full Name`",
            parse_mode="Markdown"
        )
        return

    query     = " ".join(ctx.args).lower()
    directory = load_directory()

    matches = [
        (uid, data) for uid, data in directory.items()
        if (data["username"] and query == data["username"].lower()) or
           query in data["name"].lower()
    ]

    if not matches:
        await update.message.reply_text(
            f"❌ No one matching `{query}` found in directory.\n\n"
            f"They need to send at least one message in the group first.",
            parse_mode="Markdown"
        )
        return

    lines = [f"🔍 *Lookup results for* `{query}`:\n"]
    for uid, data in matches:
        username = data["username"] or "no username"
        lines.append(f"• {data['name']} ({username}) — ID: `{uid}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_directory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔ Only the super admin can view the directory.")
        return

    directory = load_directory()
    if not directory:
        await update.message.reply_text(
            "📂 Directory is empty — no one has sent a message in the group yet."
        )
        return

    lines = [f"📂 *Directory* ({len(directory)} users seen)\n"]
    for uid, data in directory.items():
        username = data["username"] or "no username"
        lines.append(f"• {data['name']} ({username}) — `{uid}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    username = f"@{u.username}" if u.username else "no username set"
    await update.message.reply_text(
        f"👤 *{u.full_name}* ({username})\n"
        f"Your Telegram user ID is: `{u.id}`\n\n"
        f"Give this ID to your admin to be added as a member.",
        parse_mode="Markdown"
    )

# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    scheduler.start()
    log.info("Scheduler started.")

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Recall commands
    app.add_handler(CommandHandler("recall",        cmd_recall))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("remind",        cmd_remind))
    app.add_handler(CommandHandler("endrecall",     cmd_endrecall))

    # Member management
    app.add_handler(CommandHandler("setup",         cmd_setup))
    app.add_handler(CommandHandler("addmember",     cmd_addmember))
    app.add_handler(CommandHandler("removemember",  cmd_removemember))
    app.add_handler(CommandHandler("renamemember",  cmd_renamemember))
    app.add_handler(CommandHandler("listmembers",   cmd_listmembers))

    # Admin management
    app.add_handler(CommandHandler("addadmin",      cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin",   cmd_removeadmin))
    app.add_handler(CommandHandler("listadmins",    cmd_listadmins))

    # Utility
    app.add_handler(CommandHandler("lookup",        cmd_lookup))
    app.add_handler(CommandHandler("directory",     cmd_directory))
    app.add_handler(CommandHandler("myid",          cmd_myid))

    # Track acknowledgements + directory
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS,
        track_response
    ))

    log.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
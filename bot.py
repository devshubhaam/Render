import asyncio
import logging
import os
import re
from functools import wraps
from typing import Any

from aiohttp import web
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
PORT = int(os.getenv("PORT", "10000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BACKUP_CHANNEL_LINK = "https://t.me/+n5yhD-8_p79hMTQ1"
PROVIDED_BY = "@DesiBaddieHub"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram.minimal.autopost")


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if ADMIN_IDS and (user is None or user.id not in ADMIN_IDS):
            if update.effective_message:
                await update.effective_message.reply_text("Not allowed.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


STAGE_PHOTO_LINK = "photo_link"
STAGE_VIDEO_LINKS = "video_links"


def get_session(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault(
        "draft",
        {
            "stage": STAGE_PHOTO_LINK,
            "photo_link": None,
            "links": [],
        },
    )


def add_links(draft: dict[str, Any], urls: list[str]) -> None:
    for url in urls:
        if is_telegram_link(url):
            continue
        draft["links"].append(url)


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def is_telegram_link(url: str) -> bool:
    lowered = url.lower()
    return "t.me/" in lowered or "telegram.me/" in lowered or "telegram.dog/" in lowered


def chunked(items: list[str], size: int) -> list[list[str]]:
    if not items:
        return [[]]
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_output_message(photo_link: str, links_chunk: list[str]) -> str:
    video_lines = "\n\n".join(links_chunk)
    return (
        "𝗘𝘅𝗰𝗹𝘂𝘀𝗶𝘃𝗲 𝗩𝗶𝗱𝗲𝗼𝘀 😍\n\n"
        f"𝗣𝗵𝗼𝘁𝗼𝘀 👉 {photo_link}\n\n"
        "𝗩𝗶𝗱𝗲𝗼𝘀 👇\n\n"
        f"{video_lines}\n\n"
        "🔰𝗝𝗢𝗜𝗡 𝗢𝗨𝗥 𝗕𝗔𝗖𝗞𝗨𝗣 𝗖𝗛𝗔𝗡𝗡𝗘𝗟🔰\n"
        f"{BACKUP_CHANNEL_LINK}\n\n"
        f"✪ 𝗣𝗿𝗼𝘃𝗶𝗱𝗲𝗱 𝗯𝘆 :- {PROVIDED_BY}"
    ).strip()


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["draft"] = {
        "stage": STAGE_PHOTO_LINK,
        "photo_link": None,
        "links": [],
    }
    await update.effective_message.reply_text(
        "Session start ho gaya.\n\n"
        "𝗦𝗧𝗘𝗣 𝟭: JustPaste (photo) ka link bhejo.\n"
        "𝗦𝗧𝗘𝗣 𝟮: Uske turant baad video links bhejo, "
        "phir /done bhejo final post banane ke liye.\n"
        "• Telegram channel links auto-ignore honge"
    )


@admin_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("Current session cancel ho gayi.")


@admin_only
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    stage = draft.get("stage", STAGE_PHOTO_LINK)

    if stage == STAGE_PHOTO_LINK:
        await update.effective_message.reply_text("Pehle JustPaste (photo) ka link bhejo.")
        return

    photo_link = draft.get("photo_link")
    if not photo_link:
        await update.effective_message.reply_text("Pehle JustPaste (photo) ka link bhejo.")
        return

    clean_links = dedupe_keep_order(draft["links"])
    if not clean_links:
        await update.effective_message.reply_text("Kam se kam 1 video link bhejo.")
        return

    status_message = await update.effective_message.reply_text("Processing...")
    try:
        for link_group in chunked(clean_links, 5):
            await update.effective_chat.send_message(
                build_output_message(photo_link, link_group),
                disable_web_page_preview=True,
            )

        context.user_data["draft"] = {
            "stage": STAGE_PHOTO_LINK,
            "photo_link": None,
            "links": [],
        }
        await status_message.delete()
    except Exception as exc:
        logger.exception("Failed to finish session")
        await status_message.edit_text(f"Error: {exc}")


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    text = update.effective_message.text or ""

    if text.startswith("/"):
        return

    stage = draft.get("stage", STAGE_PHOTO_LINK)
    urls = URL_RE.findall(text)
    if not urls:
        return

    if stage == STAGE_PHOTO_LINK:
        draft["photo_link"] = urls[0]
        draft["stage"] = STAGE_VIDEO_LINKS
        await update.effective_message.reply_text(
            "Photo link mil gaya ✅\n\n"
            "𝗦𝗧𝗘𝗣 𝟮: Ab video links bhejo. Jab sab bhej do, /done bhejo."
        )
        return

    add_links(draft, urls)


@admin_only
async def handle_media_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    caption = update.effective_message.caption or ""

    stage = draft.get("stage", STAGE_PHOTO_LINK)
    urls = URL_RE.findall(caption)
    if not urls:
        return

    if stage == STAGE_PHOTO_LINK:
        draft["photo_link"] = urls[0]
        draft["stage"] = STAGE_VIDEO_LINKS
        await update.effective_message.reply_text(
            "Photo link mil gaya ✅\n\n"
            "𝗦𝗧𝗘𝗣 𝟮: Ab video links bhejo. Jab sab bhej do, /done bhejo."
        )
        return

    add_links(draft, urls)


async def start_health_server() -> web.AppRunner:
    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server running on port %s", PORT)
    return runner


def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media_caption))
    return app


async def run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env variable is required.")

    application = build_application()
    health_runner = await start_health_server()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started in polling mode")

    try:
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await health_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")

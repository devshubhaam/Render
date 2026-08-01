import asyncio
import json
import logging
import os
import re
from functools import wraps
from typing import Any

from aiohttp import ClientSession, FormData, web
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
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")

FIXED_TITLE = "@𝗗𝗲𝘀𝗶𝗕𝗮𝗱𝗱𝗶𝗲𝗛𝘂𝗯 𝗼𝗻 𝗧𝗲𝗹𝗲𝗴𝗿𝗮𝗺"
FIXED_DESCRIPTION = "Search @DesiBaddieHub To Watch More Spicy 🔥 Content 😍"
BACKUP_CHANNEL_LINK = "https://t.me/+n5yhD-8_p79hMTQ1"
PROVIDED_BY = "@DesiBaddieHub"

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}]+", re.IGNORECASE)
TELEGRAPH_ACCOUNT_URL = "https://api.telegra.ph/createAccount"
TELEGRAPH_CREATE_PAGE_URL = "https://api.telegra.ph/createPage"
IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram.minimal.autopost")

telegraph_access_token: str | None = None


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


STAGE_IMAGES = "images"
STAGE_LINKS = "links"


def get_session(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault(
        "draft",
        {
            "stage": STAGE_IMAGES,
            "images": [],
            "links": [],
            "telegraph_url": None,
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


def build_output_message(telegraph_url: str, links_chunk: list[str]) -> str:
    video_lines = "\n\n".join(links_chunk)
    return (
        "𝗘𝘅𝗰𝗹𝘂𝘀𝗶𝘃𝗲 𝗩𝗶𝗱𝗲𝗼𝘀 😍\n\n"
        f"𝗣𝗵𝗼𝘁𝗼𝘀 👉 {telegraph_url}\n\n"
        "𝗩𝗶𝗱𝗲𝗼𝘀 👇\n\n"
        f"{video_lines}\n\n"
        "🔰𝗝𝗢𝗜𝗡 𝗢𝗨𝗥 𝗕𝗔𝗖𝗞𝗨𝗣 𝗖𝗛𝗔𝗡𝗡𝗘𝗟🔰\n"
        f"{BACKUP_CHANNEL_LINK}\n\n"
        f"✪ 𝗣𝗿𝗼𝘃𝗶𝗱𝗲𝗱 𝗯𝘆 :- {PROVIDED_BY}"
    ).strip()


async def get_telegraph_token() -> str:
    global telegraph_access_token
    if telegraph_access_token:
        return telegraph_access_token

    payload = {
        "short_name": "desibaddiehub-bot",
        "author_name": PROVIDED_BY,
    }
    async with ClientSession() as session:
        async with session.post(TELEGRAPH_ACCOUNT_URL, data=payload, timeout=60) as response:
            data = await response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph account create failed: {data}")
    telegraph_access_token = data["result"]["access_token"]
    return telegraph_access_token


async def upload_image_to_imgbb(application: Application, file_id: str, filename: str) -> str:
    if not IMGBB_API_KEY:
        raise RuntimeError("IMGBB_API_KEY env variable is required.")

    tg_file = await application.bot.get_file(file_id)
    file_bytes = await tg_file.download_as_bytearray()

    form = FormData()
    form.add_field("key", IMGBB_API_KEY)
    form.add_field("image", bytes(file_bytes), filename=filename, content_type="image/jpeg")

    last_error: Any = None
    async with ClientSession() as session:
        for attempt in range(3):
            try:
                async with session.post(IMGBB_UPLOAD_URL, data=form, timeout=120) as response:
                    data = await response.json(content_type=None)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(2 * (attempt + 1))
                continue

            if data.get("success") and data.get("data", {}).get("url"):
                return data["data"]["url"]

            last_error = data
            await asyncio.sleep(2 * (attempt + 1))
            form = FormData()
            form.add_field("key", IMGBB_API_KEY)
            form.add_field("image", bytes(file_bytes), filename=filename, content_type="image/jpeg")

    raise RuntimeError(f"ImgBB image upload failed after retries: {last_error}")


async def create_telegraph_page(application: Application, image_items: list[dict[str, str]]) -> str:
    access_token = await get_telegraph_token()

    image_nodes: list[dict[str, Any]] = []
    for idx, item in enumerate(image_items, start=1):
        image_url = await upload_image_to_imgbb(application, item["file_id"], f"image_{idx}.jpg")
        image_nodes.append({"tag": "img", "attrs": {"src": image_url}})

    content = [
        {"tag": "p", "children": [FIXED_DESCRIPTION]},
        *image_nodes,
    ]
    payload = {
        "access_token": access_token,
        "title": FIXED_TITLE,
        "author_name": PROVIDED_BY,
        "content": json.dumps(content, ensure_ascii=False),
        "return_content": "false",
    }

    async with ClientSession() as session:
        async with session.post(TELEGRAPH_CREATE_PAGE_URL, data=payload, timeout=120) as response:
            data = await response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegraph page create failed: {data}")
    return data["result"]["url"]


@admin_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["draft"] = {
        "stage": STAGE_IMAGES,
        "images": [],
        "links": [],
        "telegraph_url": None,
    }
    await update.effective_message.reply_text(
        "Session start ho gaya.\n\n"
        "𝗦𝗧𝗘𝗣 𝟭: Photos/image documents bhejo, phir /done bhejo.\n"
        "𝗦𝗧𝗘𝗣 𝟮: Uske baad video links bhejo (text ya photo+caption dono chalega), "
        "phir /done phir se bhejo final post banane ke liye.\n"
        "• Telegram channel links auto-ignore honge"
    )


@admin_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text("Current session cancel ho gayi.")


@admin_only
async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    stage = draft.get("stage", STAGE_IMAGES)

    if stage == STAGE_IMAGES:
        if not draft["images"]:
            await update.effective_message.reply_text("Kam se kam 1 image bhejo.")
            return

        status_message = await update.effective_message.reply_text("Photos upload ho rahi hain...")
        try:
            telegraph_url = await create_telegraph_page(context.application, draft["images"])
            draft["telegraph_url"] = telegraph_url
            draft["stage"] = STAGE_LINKS
            draft["images"] = []
            await status_message.edit_text(
                "Photos upload ho gaye ✅\n"
                f"Telegraph: {telegraph_url}\n\n"
                "𝗦𝗧𝗘𝗣 𝟮: Ab video links bhejo (text ya photo+caption dono chalega).\n"
                "Jab sab bhej do, /done phir se bhejo."
            )
        except Exception as exc:
            logger.exception("Failed to create telegraph page")
            await status_message.edit_text(f"Error: {exc}")
        return

    telegraph_url = draft.get("telegraph_url")
    if not telegraph_url:
        await update.effective_message.reply_text("Pehle Step 1 complete karo: images bhejo aur /done karo.")
        return

    clean_links = dedupe_keep_order(draft["links"])
    if not clean_links:
        await update.effective_message.reply_text("Kam se kam 1 video link bhejo.")
        return

    status_message = await update.effective_message.reply_text("Processing...")
    try:
        for link_group in chunked(clean_links, 5):
            await update.effective_chat.send_message(
                build_output_message(telegraph_url, link_group),
                disable_web_page_preview=True,
            )

        context.user_data["draft"] = {
            "stage": STAGE_IMAGES,
            "images": [],
            "links": [],
            "telegraph_url": None,
        }
        await status_message.delete()
    except Exception as exc:
        logger.exception("Failed to finish session")
        await status_message.edit_text(f"Error: {exc}")


@admin_only
async def handle_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    message = update.effective_message
    stage = draft.get("stage", STAGE_IMAGES)

    is_image = bool(message.photo) or (
        message.document and (message.document.mime_type or "").startswith("image/")
    )

    if stage == STAGE_IMAGES:
        if is_image:
            file_id = message.photo[-1].file_id if message.photo else message.document.file_id
            draft["images"].append({"file_id": file_id})
        return

    # Stage 2: image ka use nahi, sirf caption me se video link nikalna hai
    caption = message.caption or ""
    urls = URL_RE.findall(caption)
    if urls:
        add_links(draft, urls)


@admin_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_session(context)
    text = update.effective_message.text or ""

    if text.startswith("/"):
        return

    stage = draft.get("stage", STAGE_IMAGES)
    if stage != STAGE_LINKS:
        return

    urls = URL_RE.findall(text)
    if not urls:
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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_images))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
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

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
 
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv
import yt_dlp
 
load_dotenv()
 
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # "@channelusername" или "-100xxxxxxxxxx"
 
_owner_id_raw = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID = int(_owner_id_raw) if _owner_id_raw else 0
except ValueError:
    raise ValueError(
        f"OWNER_ID в .env должен быть числом (твой Telegram ID), а сейчас там: {_owner_id_raw!r}. "
        "Узнать свой числовой ID можно у @userinfobot — впиши именно число, без @ и кавычек."
    )
 
TIKTOK_RE = re.compile(r"(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+", re.IGNORECASE)
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ — лимит на загрузку файла ботом в Telegram
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
 
router = Router()
 
 
class DebugLoggingMiddleware(BaseMiddleware):
    """
    Логирует КАЖДОЕ входящее сообщение до применения фильтров.
    Это нужно, чтобы в консоли сразу было видно: дошло ли сообщение до бота,
    из какого чата, от какого user_id, и совпадает ли текст с ожидаемой ссылкой.
    Ничего не перехватывает — просто пропускает событие дальше.
    """
 
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            logging.info(
                "ВХОДЯЩЕЕ СООБЩЕНИЕ: chat_type=%s, chat_id=%s, from_user_id=%s, text=%r",
                event.chat.type,
                event.chat.id,
                event.from_user.id if event.from_user else None,
                event.text,
            )
        return await handler(event, data)
 
 
def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID
 
 
@router.message(CommandStart())
async def cmd_start(message: Message):
    # === ЗАЩИТА: бот отвечает и реагирует только на владельца ===
    if message.from_user is None or not is_owner(message.from_user.id):
        return
    await message.answer(
        "Привет! Пришли мне ссылку на видео из TikTok — я скачаю его и опубликую в твой канал."
    )
 
 
@router.message(F.text.regexp(TIKTOK_RE.pattern))
async def handle_tiktok_link(message: Message, bot: Bot):
    # === ЗАЩИТА: любой, кто не владелец, полностью игнорируется/отклоняется ===
    if message.from_user is None:
        logging.warning("Сообщение со ссылкой пришло без from_user (например, пост в канале) — игнорирую.")
        return
 
    if not is_owner(message.from_user.id):
        logging.warning(
            f"Попытка использовать бота посторонним: user_id={message.from_user.id}, "
            f"username=@{message.from_user.username}"
        )
        await message.answer("⛔️ У вас нет доступа к этому боту.")
        return
 
    match = TIKTOK_RE.search(message.text)
    if not match:
        return
    url = match.group(0)
    if not url.lower().startswith("http"):
        url = "https://" + url  # ссылка без протокола (например, скопированная как "vt.tiktok.com/xxxx")
 
    status_msg = await message.answer("⏳ Скачиваю видео...")
 
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_template = str(Path(tmp_dir) / "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": output_template,
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
 
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, True)
                file_path = Path(ydl.prepare_filename(info))
                if not file_path.exists():
                    # иногда после merge меняется расширение файла
                    candidates = list(Path(tmp_dir).glob("*.mp4"))
                    if candidates:
                        file_path = candidates[0]
        except Exception as e:
            logging.error(f"Ошибка скачивания видео: {e}")
            await status_msg.edit_text(
                "❌ Не удалось скачать видео. Проверь ссылку (видео могло быть удалено "
                "или быть приватным) и попробуй снова."
            )
            return
 
        if not file_path.exists():
            await status_msg.edit_text("❌ Файл не найден после скачивания.")
            return
 
        file_size = file_path.stat().st_size
        if file_size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                f"❌ Видео весит {file_size / 1024 / 1024:.1f} МБ — это больше лимита "
                f"Telegram Bot API на загрузку файла (50 МБ)."
            )
            return
 
        caption = (info.get("description") or "")[:1000]  # лимит подписи в Telegram
 
        try:
            await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(file_path),
                caption=caption,
            )
        except Exception as e:
            logging.error(f"Ошибка отправки в канал: {e}")
            await status_msg.edit_text(
                "❌ Не удалось отправить видео в канал. Проверь, что бот добавлен в канал "
                "как администратор с правом публикации сообщений."
            )
            return
 
    await status_msg.edit_text("✅ Видео опубликовано в канале!")
 
 
@router.message()
async def fallback(message: Message):
    # === ЗАЩИТА: и на любые прочие сообщения посторонним бот не отвечает ===
    if message.from_user is None or not is_owner(message.from_user.id):
        return
    await message.answer("Пришли ссылку на видео из TikTok (tiktok.com/...).")
 
 
async def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN не задан. Проверь, что рядом с bot.py лежит файл именно '.env' "
            "(не '.env.example'), и в нём заполнена строка BOT_TOKEN=..."
        )
    if OWNER_ID == 0:
        raise ValueError(
            "OWNER_ID не задан или равен 0. Узнай свой числовой Telegram ID у @userinfobot "
            "и впиши его в .env как OWNER_ID=123456789 (без @, без кавычек)."
        )
    if not CHANNEL_ID:
        raise ValueError("CHANNEL_ID не задан в .env!")
 
    logging.info(f"Запуск с OWNER_ID={OWNER_ID}, CHANNEL_ID={CHANNEL_ID}")
 
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.message.outer_middleware(DebugLoggingMiddleware())
    dp.include_router(router)
 
    me = await bot.get_me()
    logging.info(f"Бот запущен как @{me.username} (id={me.id})")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
 
 
if __name__ == "__main__":
    asyncio.run(main())

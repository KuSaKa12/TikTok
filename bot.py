import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
 
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from dotenv import load_dotenv
import yt_dlp
 
load_dotenv()
 
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")  # "@channelusername" или "-100xxxxxxxxxx"
 
TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+", re.IGNORECASE)
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 МБ — лимит на загрузку файла ботом в Telegram
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
 
router = Router()
 
 
def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID
 
 
@router.message(CommandStart())
async def cmd_start(message: Message):
    # === ЗАЩИТА: бот отвечает и реагирует только на владельца ===
    if not is_owner(message.from_user.id):
        return
    await message.answer(
        "Привет! Пришли мне ссылку на видео из TikTok — я скачаю его и опубликую в твой канал."
    )
 
 
@router.message(F.text.regexp(TIKTOK_RE.pattern))
async def handle_tiktok_link(message: Message, bot: Bot):
    # === ЗАЩИТА: любой, кто не владелец, полностью игнорируется/отклоняется ===
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
    if not is_owner(message.from_user.id):
        return
    await message.answer("Пришли ссылку на видео из TikTok (tiktok.com/...).")
 
 
async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в переменных окружения (.env)!")
    if OWNER_ID == 0:
        raise ValueError("OWNER_ID не задан в переменных окружения (.env)!")
    if not CHANNEL_ID:
        raise ValueError("CHANNEL_ID не задан в переменных окружения (.env)!")
 
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
 
    logging.info("Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)
 
 if __name__ == "__main__":
    asyncio.run(main())

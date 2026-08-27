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
import aiohttp
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
TIKWM_API = "https://www.tikwm.com/api/"  # резервный способ скачивания, если TikTok блокирует IP сервера

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


async def download_via_tikwm(url: str, dest_path: Path) -> str:
    """
    Резервный способ скачивания на случай, если TikTok блокирует прямые запросы
    с IP сервера (частая ситуация для облачных/датацентровых IP). Обращается
    к публичному API tikwm.com — оно само запрашивает видео у TikTok со своих
    серверов и отдаёт прямую ссылку на mp4 без вотермарки.
    Возвращает подпись (описание видео), файл сохраняется в dest_path.
    """
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(TIKWM_API, params={"url": url}) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

        if data.get("code") != 0:
            raise RuntimeError(f"tikwm API вернул ошибку: {data.get('msg')}")

        video_data = data.get("data") or {}
        video_url = video_data.get("play") or video_data.get("hdplay") or video_data.get("wmplay")
        if not video_url:
            raise RuntimeError("tikwm API не вернул ссылку на видео")
        if not video_url.startswith("http"):
            video_url = "https://www.tikwm.com" + video_url

        async with session.get(video_url) as video_resp:
            video_resp.raise_for_status()
            with open(dest_path, "wb") as f:
                async for chunk in video_resp.content.iter_chunked(256 * 1024):
                    f.write(chunk)

        return video_data.get("title", "")


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
        file_path = Path(tmp_dir) / "video.mp4"
        caption = ""
        downloaded = False

        # --- Способ 1: yt-dlp напрямую ---
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
                candidate = Path(ydl.prepare_filename(info))
                if not candidate.exists():
                    # иногда после merge меняется расширение файла
                    candidates = list(Path(tmp_dir).glob("*.mp4"))
                    if candidates:
                        candidate = candidates[0]
                if candidate.exists():
                    file_path = candidate
                    downloaded = True
        except Exception as e:
            logging.warning(f"yt-dlp не смог скачать напрямую ({e}), пробую резервный способ через tikwm.com...")

        # --- Способ 2 (резерв): tikwm.com, если TikTok заблокировал IP сервера ---
        if not downloaded:
            try:
                title = await download_via_tikwm(url, file_path)
                downloaded = True
            except Exception as e2:
                logging.error(f"Резервный способ скачивания тоже не сработал: {e2}")
                await status_msg.edit_text(
                    "❌ Не удалось скачать видео ни одним из способов. Проверь ссылку "
                    "(видео могло быть удалено/приватным) или попробуй ещё раз позже."
                )
                return

        if not downloaded or not file_path.exists():
            await status_msg.edit_text("❌ Файл не найден после скачивания.")
            return

        file_size = file_path.stat().st_size
        if file_size > MAX_TELEGRAM_FILE_SIZE:
            await status_msg.edit_text(
                f"❌ Видео весит {file_size / 1024 / 1024:.1f} МБ — это больше лимита "
                f"Telegram Bot API на загрузку файла (50 МБ)."
            )
            return

        try:
            await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(file_path),
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

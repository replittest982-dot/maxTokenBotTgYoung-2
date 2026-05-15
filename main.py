import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright, Page, BrowserContext

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    token: str = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")
    target_url: str = "https://web.max.ru"
    validate_url: str = "https://api.max.ru/v1/users/me"
    max_retries: int = 3
    scan_timeout_sec: int = 90
    poll_interval_sec: int = 2
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    tmp_dir: Path = Path("/tmp/bot_sessions")

CFG = Config()
CFG.tmp_dir.mkdir(parents=True, exist_ok=True)

bot = Bot(token=CFG.token)
dp = Dispatcher()

# chat_id пользователей с активной сессией — защита от параллельных запусков
_active_sessions: set[int] = set()

# ─── JS: читаем localStorage после авторизации ────────────────────────────────

EXTRACT_JS = """
() => {
    const deviceId = localStorage.getItem('__oneme_device_id')
        || localStorage.getItem('oneme_device_id');
    const authRaw  = localStorage.getItem('__oneme_auth');
    if (!deviceId || !authRaw) return null;
    let auth;
    try { auth = JSON.parse(authRaw); } catch(e) { return null; }
    if (!auth.token) return null;
    return JSON.stringify({
        device_id: deviceId,
        viewer_id: auth.viewerId || null,
        token: auth.token,
        raw_auth: auth
    });
}
"""

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def tmp_path(name: str) -> Path:
    return CFG.tmp_dir / name

def build_txt(data: dict) -> str:
    auth_json = json.dumps(
        {"viewerId": data.get("viewer_id"), "token": data["token"]},
        ensure_ascii=False,
    )
    return (
        f"localStorage.clear();\n"
        f"localStorage.setItem('__oneme_device_id', '{data['device_id']}');\n"
        f"localStorage.setItem('oneme_device_id', '{data['device_id']}');\n"
        f"localStorage.setItem('__oneme_auth', '{auth_json}');\n"
        f"location.reload();\n"
    )

def build_json(data: dict) -> str:
    return json.dumps(
        {"token": data["token"], "device_id": data["device_id"], "viewer_id": data.get("viewer_id")},
        ensure_ascii=False,
        indent=2,
    )

async def take_qr_screenshot(page: Page, chat_id: int, attempt: int) -> Path:
    path = tmp_path(f"qr_{chat_id}_{attempt}.png")
    for sel in ["canvas", "img[src*='qr']", ".qr", "#qr", "[data-testid='qr']"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.screenshot(path=str(path))
                return path
        except Exception:
            continue
    await page.screenshot(path=str(path))
    return path

async def wait_for_auth(page: Page) -> dict | None:
    for _ in range(CFG.scan_timeout_sec // CFG.poll_interval_sec):
        await asyncio.sleep(CFG.poll_interval_sec)
        try:
            raw = await page.evaluate(EXTRACT_JS)
            if raw:
                return json.loads(raw)
        except Exception:
            continue
    return None

# ─── Валидация токена ─────────────────────────────────────────────────────────

async def validate_token(token: str, device_id: str) -> bool:
    """Проверяет валидность токена сессии MAX через GET /v1/users/me"""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Device-Id": device_id,
        "User-Agent": CFG.user_agent,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                CFG.validate_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                is_valid = resp.status == 200
                logger.info("Валидация токена: HTTP %s → %s", resp.status, "OK" if is_valid else "FAIL")
                return is_valid
    except Exception as e:
        logger.warning("Ошибка при валидации токена: %s", e)
        return False

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Получить сессию", callback_data="auth")
    kb.button(text="❓ Помощь", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()

def after_auth_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Получить новую сессию", callback_data="auth")
    kb.button(text="🏠 Главное меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()

def back_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()

# ─── Основная логика ──────────────────────────────────────────────────────────

async def get_session_data(chat_id: int) -> None:
    # Защита от параллельных запусков для одного и того же пользователя
    if chat_id in _active_sessions:
        await bot.send_message(chat_id, "⏳ Сессия уже запускается, подожди окончания предыдущего процесса.")
        return
        
    _active_sessions.add(chat_id)
    
    try:
        async with async_playwright() as p:
            # Chromium с минимальным потреблением RAM
            browser = await p.chromium.launch(
                headless=CFG.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",   # не использовать /dev/shm (важно при низком RAM)
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-translate",
                    "--mute-audio",
                    "--no-first-run",
                    "--js-flags=--max-old-space-size=256",  # лимит V8 heap 256 МБ
                ],
            )
            context: BrowserContext = await browser.new_context(
                user_agent=CFG.user_agent,
                java_script_enabled=True,
                # Блокируем тяжёлые ресурсы для экономии RAM и трафика
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
            )

            # Блокируем тяжёлые ресурсы для экономии RAM.
            # stylesheet НЕ блокируем — CSS нужен для корректного рендера QR.
            # image блокируем глобально, кроме URL содержащих "qr" (на случай img-QR).
            await context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("media", "font")
                or (
                    route.request.resource_type == "image"
                    and "qr" not in route.request.url
                )
                else route.continue_(),
            )

            page: Page = await context.new_page()

            await bot.send_message(chat_id, "🔄 Запускаю браузер, загружаю web.max.ru...")
            await page.goto(CFG.target_url, wait_until="networkidle", timeout=30_000)

            for attempt in range(CFG.max_retries):
                await asyncio.sleep(3)

                qr_path = await take_qr_screenshot(page, chat_id, attempt)
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=FSInputFile(str(qr_path)),
                        caption=(
                            f"📲 Отсканируй QR в приложении MAX "
                            f"(попытка {attempt + 1}/{CFG.max_retries})\n"
                            f"⏳ Жду {CFG.scan_timeout_sec} сек..."
                        ),
                    )
                finally:
                    qr_path.unlink(missing_ok=True)

                data = await wait_for_auth(page)

                if data:
                    # ── Валидация токена ───────────────────────────────────
                    await bot.send_message(chat_id, "🔍 Проверяю валидность токена...")
                    is_valid = await validate_token(data["token"], data["device_id"])

                    if is_valid:
                        await bot.send_message(chat_id, "✅ Токен валиден — сессия рабочая!")
                    else:
                        await bot.send_message(
                            chat_id,
                            "⚠️ Токен получен, но не прошёл проверку.\n"
                            "Файлы всё равно отправляю — возможно, эндпоинт недоступен."
                        )

                    # ── Сохраняем файлы ───────────────────────────────────
                    txt_path  = tmp_path(f"session_{chat_id}.txt")
                    json_path = tmp_path(f"session_{chat_id}.json")
                    txt_path.write_text(build_txt(data),  encoding="utf-8")
                    json_path.write_text(build_json(data), encoding="utf-8")

                    await bot.send_message(chat_id, "📤 Отправляю файлы...")

                    try:
                        await bot.send_document(
                            chat_id=chat_id,
                            document=FSInputFile(str(txt_path)),
                            caption="📄 localStorage (вставь в консоль браузера)",
                        )
                        await bot.send_document(
                            chat_id=chat_id,
                            document=FSInputFile(str(json_path)),
                            caption="📋 JSON сессии",
                        )
                    finally:
                        txt_path.unlink(missing_ok=True)
                        json_path.unlink(missing_ok=True)

                    await bot.send_message(
                        chat_id,
                        "🎉 Готово! Оба файла у тебя.\n\nЧто дальше?",
                        reply_markup=after_auth_menu(),
                    )
                    return

                # Не авторизовались — следующая попытка
                if attempt < CFG.max_retries - 1:
                    await bot.send_message(chat_id, "⏳ Время вышло. Обновляю QR...")
                    await page.reload(wait_until="networkidle")
                else:
                    await bot.send_message(chat_id, "❌ Все попытки исчерпаны. Попробуй /start снова.")
                    
            # Закрываем контекст и браузер явно перед выходом
            await context.close()
            await browser.close()

    except Exception as e:
        logger.exception("Ошибка для chat_id=%s", chat_id)
        await bot.send_message(chat_id, f"⚠️ Ошибка: {e}")
    finally:
        # Убираем пользователя из сета после завершения или ошибки
        _active_sessions.discard(chat_id)

# ─── Handlers ─────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "📖 *Справка*\n\n"
    "1️⃣ Нажми *Получить сессию*\n"
    "2️⃣ Дождись QR-кода\n"
    "3️⃣ Отсканируй его в приложении MAX\n"
    "4️⃣ Получи два файла:\n"
    "   • `session.txt` — вставить в консоль браузера\n"
    "   • `session.json` — токен и device\_id\n\n"
    "Можно повторять сколько угодно раз."
)

@dp.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    await message.answer(
        "👋 *Привет!*\n\n"
        "Я помогу получить данные сессии твоего аккаунта MAX.\n\n"
        "Нажми кнопку ниже чтобы начать 👇",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )

@dp.message(Command("auth"))
async def cmd_auth(message: types.Message) -> None:
    if message.chat.id in _active_sessions:
        await message.answer("⏳ Процесс уже запущен, ожидай завершения!")
        return
    await message.answer("⚙️ Запускаю авторизацию, жди QR-код...")
    asyncio.create_task(get_session_data(message.chat.id))

@dp.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="Markdown", reply_markup=back_menu())

# ─── Callback handlers ────────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu")
async def cb_menu(call: types.CallbackQuery) -> None:
    await call.message.edit_text(
        "👋 *Главное меню*\n\nВыбери действие 👇",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )
    await call.answer()

@dp.callback_query(F.data == "auth")
async def cb_auth(call: types.CallbackQuery) -> None:
    if call.message.chat.id in _active_sessions:
        await call.answer("⏳ Процесс уже запущен!", show_alert=True)
        return
    await call.message.edit_text("⚙️ Запускаю авторизацию, жди QR-код...", reply_markup=None)
    await call.answer()
    asyncio.create_task(get_session_data(call.message.chat.id))

@dp.callback_query(F.data == "help")
async def cb_help(call: types.CallbackQuery) -> None:
    await call.message.edit_text(HELP_TEXT, parse_mode="Markdown", reply_markup=back_menu())
    await call.answer()

# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("Бот успешно запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

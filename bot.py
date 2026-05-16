import asyncio
import json
import logging
import os
import time
import random
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from playwright.async_api import async_playwright, Page, BrowserContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    token: str = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_БОТА")
    target_url: str = "https://web.max.ru"
    max_retries: int = 3
    scan_timeout_sec: int = 90
    poll_interval_sec: int = 2
    headless: bool = True

    web_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    mobile_user_agent: str = "Dalvik/2.1.0 (Linux; U; Android 13; SM-S911B Build/TP1A.220624.014)"

    tmp_dir: Path = Path("/tmp/bot_sessions")

CFG = Config()
CFG.tmp_dir.mkdir(parents=True, exist_ok=True)

bot = Bot(token=CFG.token)
dp = Dispatcher()

_active_sessions: set[int] = set()

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

def build_json(data: dict, format_type: str) -> str:
    if format_type == "mobile":
        session_id = int(time.time() * 1000) + random.randint(100, 999)
        client_session_id = random.randint(5, 25)
        mobile_format = {
            "token": data["token"],
            "device_id": data["device_id"],
            "session_id": session_id,
            "client_session_id": client_session_id,
            "password": None,
            "connection_params": {
                "device_type": "ANDROID",
                "app_version": "26.14.1",
                "device_name": "Samsung Galaxy S23 Ultra",
                "os_version": "Android 13",
                "build_number": 2569,
                "locale": "ru",
                "device_locale": "ru",
                "screen": "412x915 3.0x",
                "timezone": "Europe/Moscow",
                "header_user_agent": CFG.mobile_user_agent,
                "release": 1
            }
        }
        return json.dumps(mobile_format, ensure_ascii=False, indent=2)
    else:
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

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Получить сессию", callback_data="auth")
    kb.button(text="❓ Помощь", callback_data="help")
    kb.adjust(1)
    return kb.as_markup()

def format_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🖥 Web-версия", callback_data="start_auth_web")
    kb.button(text="📱 Мобильная версия", callback_data="start_auth_mobile")
    kb.button(text="🔙 Назад", callback_data="menu")
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

async def get_session_data(chat_id: int, format_type: str) -> None:
    if chat_id in _active_sessions:
        await bot.send_message(chat_id, "⏳ Сессия уже запускается, подожди окончания предыдущего процесса.")
        return

    _active_sessions.add(chat_id)

    current_user_agent = CFG.mobile_user_agent if format_type == "mobile" else CFG.web_user_agent
    viewport = {"width": 412, "height": 915} if format_type == "mobile" else {"width": 1280, "height": 720}
    is_mobile = format_type == "mobile"
    format_name = "Мобильная" if is_mobile else "Web"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=CFG.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-sync",
                    "--disable-translate",
                    "--disable-default-apps",
                    "--disable-hang-monitor",
                    "--disable-prompt-on-repost",
                    "--disable-client-side-phishing-detection",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
                    "--mute-audio",
                    "--no-first-run",
                    "--no-zygote",              # экономит ~100 МБ RAM
                    "--single-process",         # один процесс вместо нескольких​​​​​​​​​​​​​​​​

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from opentele2.api import API
from opentele2.tl import TelegramClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

log = logging.getLogger("telegram-join-manager.bootstrap")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
AUTH_VERSION = 4
API_TEMPLATE = "TelegramDesktop"


def build_user_api(owner_id: int):
    """Return the same official-client fingerprint on every runner."""
    return API.TelegramDesktop.Generate(
        unique_id=f"telegram-join-manager-v4-user-{owner_id}"
    )


def build_bot_api(owner_id: int):
    """Stable but separate fingerprint for the MTProto bot connection."""
    return API.TelegramDesktop.Generate(
        unique_id=f"telegram-join-manager-v4-bot-{owner_id}"
    )


@dataclass(frozen=True)
class AuthConfig:
    user_session: str
    api_template: str = API_TEMPLATE
    version: int = AUTH_VERSION


class AuthVault:
    """Encrypt the user MTProto session before persisting it in a private repository."""

    def __init__(self, path: Path, bot_token: str, owner_id: int, persist_to_git: bool) -> None:
        self.path = path
        self.root = path.resolve().parents[1]
        self.persist_to_git = persist_to_git
        seed = f"telegram-join-manager-v4\0{owner_id}\0{bot_token}".encode("utf-8")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
        self.fernet = Fernet(key)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def load(self) -> AuthConfig:
        token = self.path.read_bytes().strip()
        try:
            raw = json.loads(self.fernet.decrypt(token).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("فایل احراز هویت قابل خواندن نیست") from exc

        # V4 intentionally refuses old V3 auth payloads because they were created
        # with a different API/fingerprint and should not be silently reused.
        if int(raw.get("version", 0)) != AUTH_VERSION:
            raise RuntimeError("AUTH_VERSION_MISMATCH")
        session = str(raw.get("user_session", "")).strip()
        if not session:
            raise RuntimeError("EMPTY_USER_SESSION")
        return AuthConfig(
            user_session=session,
            api_template=str(raw.get("api_template", API_TEMPLATE)),
            version=AUTH_VERSION,
        )

    async def save(self, config: AuthConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(config), separators=(",", ":")).encode("utf-8")
        encrypted = self.fernet.encrypt(payload)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        tmp.replace(self.path)
        if self.persist_to_git and os.getenv("GITHUB_ACTIONS") == "true":
            await asyncio.to_thread(self._git_commit, "Save encrypted Telegram authorization")

    def delete_local(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.exception("Could not delete auth vault")

    def _git_commit(self, message: str) -> None:
        try:
            relative = str(self.path.relative_to(self.root))
            subprocess.run(["git", "add", relative], cwd=self.root, check=True)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=self.root, check=False
            )
            if diff.returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", message], cwd=self.root, check=True)
            subprocess.run(
                ["git", "pull", "--rebase", "--autostash"], cwd=self.root, check=True
            )
            subprocess.run(["git", "push"], cwd=self.root, check=True)
        except subprocess.CalledProcessError:
            log.exception("Could not persist encrypted auth to git")


class SetupWizard:
    def __init__(self, bot_token: str, owner_id: int, vault: AuthVault) -> None:
        self.bot_token = bot_token
        self.owner_id = owner_id
        self.vault = vault
        self.state = "idle"
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.user_client: TelegramClient | None = None
        self.completed = asyncio.Event()
        self.result: AuthConfig | None = None

    def is_owner(self, update: Update) -> bool:
        return bool(
            update.effective_user
            and update.effective_user.id == self.owner_id
            and update.effective_chat
            and update.effective_chat.type == "private"
        )

    async def reject_non_owner(self, update: Update) -> None:
        if update.effective_message:
            await update.effective_message.reply_text("این ربات خصوصی است.")

    async def safe_delete(self, update: Update) -> None:
        try:
            if update.effective_message:
                await update.effective_message.delete()
        except Exception:  # noqa: BLE001
            pass

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            await self.reject_non_owner(update)
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📱 اتصال اکانت مدیر", callback_data="setup:start")]]
        )
        text = (
            "<b>راه‌اندازی Telegram Join Manager V4</b>\n\n"
            "برای این نسخه فقط <code>BOT_TOKEN</code> و <code>OWNER_ID</code> در GitHub لازم است.\n\n"
            "برای اتصال اکانت مدیر فقط شماره تلفن را می‌گیریم؛ سپس کد ورود Telegram و در صورت "
            "فعال‌بودن تأیید دومرحله‌ای، رمز را دریافت می‌کنیم. Session ساخته‌شده رمزگذاری و ذخیره می‌شود.\n\n"
            "پیام‌های شامل شماره، کد و رمز بعد از دریافت تا حد امکان حذف می‌شوند."
        )
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        await self._reset_client()
        self._reset_fields()
        await update.effective_message.reply_text(
            "راه‌اندازی لغو شد. برای شروع دوباره /start را بزن."
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        query = update.callback_query
        if not query:
            return
        await query.answer()
        if query.data == "setup:start":
            await self._reset_client()
            self._reset_fields()
            self.state = "phone"
            await query.edit_message_text(
                "<b>مرحله ۱ از ۳</b>\n\n"
                "شماره اکانت مدیر را با کد کشور بفرست؛ مثال:\n"
                "<code>+989121234567</code>\n\n"
                "API_ID/API_HASH از تو خواسته نمی‌شود.",
                parse_mode=ParseMode.HTML,
            )
        elif query.data == "setup:resend" and self.phone:
            await self._send_login_code(context, self.phone, resend=True)
        elif query.data == "setup:restart":
            await self._reset_client()
            self._reset_fields()
            self.state = "phone"
            await query.edit_message_text(
                "شماره را دوباره با کد کشور بفرست؛ مثال: <code>+989121234567</code>",
                parse_mode=ParseMode.HTML,
            )

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            await self.reject_non_owner(update)
            return
        text = (update.effective_message.text or "").strip()
        if self.state == "idle":
            await update.effective_message.reply_text("برای شروع /start را بزن.")
            return

        if self.state == "phone":
            await self.safe_delete(update)
            phone = re.sub(r"[\s()-]", "", text)
            if not PHONE_RE.fullmatch(phone):
                await context.bot.send_message(
                    self.owner_id,
                    "شماره معتبر نیست. با + و کد کشور بفرست؛ مثال: <code>+989121234567</code>",
                    parse_mode=ParseMode.HTML,
                )
                return
            await self._send_login_code(context, phone)
            return

        if self.state == "code":
            await self.safe_delete(update)
            code = re.sub(r"\D", "", text)
            if len(code) < 4:
                await context.bot.send_message(self.owner_id, "کد ورود معتبر نیست. دوباره بفرست.")
                return
            await self._sign_in_code(context, code)
            return

        if self.state == "password":
            await self.safe_delete(update)
            if not text:
                await context.bot.send_message(self.owner_id, "رمز خالی است. دوباره بفرست.")
                return
            await self._sign_in_password(context, text)
            return

    async def _new_user_client(self) -> TelegramClient:
        api = build_user_api(self.owner_id)
        client = TelegramClient(StringSession(), api=api)
        await client.connect()
        return client

    async def _send_login_code(
        self, context: ContextTypes.DEFAULT_TYPE, phone: str, resend: bool = False
    ) -> None:
        await self._reset_client()
        try:
            self.user_client = await self._new_user_client()
            sent = await self.user_client.send_code_request(phone, force_sms=False)
        except PhoneNumberInvalidError:
            await self._reset_client()
            self.state = "phone"
            await context.bot.send_message(self.owner_id, "شماره تلگرام نامعتبر است. دوباره بفرست.")
            return
        except PhoneNumberBannedError:
            await self._reset_client()
            self.state = "phone"
            await context.bot.send_message(self.owner_id, "Telegram این شماره را برای ورود API مسدود کرده است.")
            return
        except FloodWaitError as exc:
            await self._reset_client()
            self.state = "phone"
            await context.bot.send_message(
                self.owner_id,
                f"Telegram محدودیت موقت داده است. {exc.seconds} ثانیه بعد دوباره تلاش کن.",
            )
            return
        except Exception as exc:  # noqa: BLE001
            await self._reset_client()
            self.state = "phone"
            log.exception("Could not send login code")
            await context.bot.send_message(
                self.owner_id,
                "ارسال کد ناموفق بود: "
                f"<code>{type(exc).__name__}</code>\n\n"
                "اگر روی GitHub Actions کد دریافت نشد، /start را دوباره امتحان کن؛ "
                "Telegram گاهی ورود از IPهای دیتاسنتری را محدود می‌کند.",
                parse_mode=ParseMode.HTML,
            )
            return

        self.phone = phone
        self.phone_code_hash = sent.phone_code_hash
        self.state = "code"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 ارسال دوباره کد", callback_data="setup:resend")],
                [InlineKeyboardButton("📱 تغییر شماره", callback_data="setup:restart")],
            ]
        )
        prefix = "کد جدید درخواست شد." if resend else "کد ورود درخواست شد."
        await context.bot.send_message(
            self.owner_id,
            f"<b>مرحله ۲ از ۳</b>\n\n{prefix}\n"
            "کدی که Telegram برای اکانتت فرستاده را همین‌جا بفرست. فاصله و خط تیره مهم نیست.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    async def _sign_in_code(self, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
        if not self.user_client or not self.phone or not self.phone_code_hash:
            self.state = "phone"
            await context.bot.send_message(self.owner_id, "نشست موقت از بین رفته؛ شماره را دوباره بفرست.")
            return
        try:
            await self.user_client.sign_in(
                phone=self.phone,
                code=code,
                phone_code_hash=self.phone_code_hash,
            )
        except SessionPasswordNeededError:
            self.state = "password"
            await context.bot.send_message(
                self.owner_id,
                "<b>مرحله ۳ از ۳</b>\n\n"
                "تأیید دومرحله‌ای فعال است. رمز دومرحله‌ای Telegram را بفرست.",
                parse_mode=ParseMode.HTML,
            )
            return
        except PhoneCodeInvalidError:
            await context.bot.send_message(self.owner_id, "کد اشتباه است. دوباره بفرست.")
            return
        except PhoneCodeExpiredError:
            await self._reset_client()
            self.state = "phone"
            await context.bot.send_message(self.owner_id, "کد منقضی شده؛ شماره را دوباره بفرست.")
            return
        except FloodWaitError as exc:
            await context.bot.send_message(
                self.owner_id, f"محدودیت موقت Telegram: {exc.seconds} ثانیه بعد دوباره تلاش کن."
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Sign in with code failed")
            await context.bot.send_message(
                self.owner_id, f"ورود ناموفق بود: <code>{type(exc).__name__}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await self._finish(context)

    async def _sign_in_password(self, context: ContextTypes.DEFAULT_TYPE, password: str) -> None:
        if not self.user_client:
            self.state = "phone"
            await context.bot.send_message(self.owner_id, "نشست موقت از بین رفته؛ شماره را دوباره بفرست.")
            return
        try:
            await self.user_client.sign_in(password=password)
        except PasswordHashInvalidError:
            await context.bot.send_message(self.owner_id, "رمز دومرحله‌ای اشتباه است. دوباره بفرست.")
            return
        except FloodWaitError as exc:
            await context.bot.send_message(
                self.owner_id, f"محدودیت موقت Telegram: {exc.seconds} ثانیه بعد دوباره تلاش کن."
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("2FA sign in failed")
            await context.bot.send_message(
                self.owner_id,
                f"ورود دومرحله‌ای ناموفق بود: <code>{type(exc).__name__}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await self._finish(context)

    async def _finish(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert self.user_client is not None
        if not await self.user_client.is_user_authorized():
            await context.bot.send_message(self.owner_id, "احراز هویت کامل نشد. /start را دوباره بزن.")
            return

        me = await self.user_client.get_me()
        if int(me.id) != self.owner_id:
            await context.bot.send_message(
                self.owner_id,
                "⛔ این شماره متعلق به OWNER_ID ثبت‌شده نیست. برای جلوگیری از ذخیره Session حساب دیگری، عملیات لغو شد.",
            )
            await self._reset_client()
            self._reset_fields()
            return

        session = StringSession.save(self.user_client.session)
        config = AuthConfig(user_session=session)
        await self.vault.save(config)
        self.result = config
        self.state = "done"
        await context.bot.send_message(
            self.owner_id,
            "✅ اتصال اکانت کامل شد. Session رمزگذاری و ذخیره شد.\n"
            "از این به بعد اجرای GitHub Actions بدون شماره و کد وارد پنل اصلی می‌شود.\n\n"
            "در حال انتقال به پنل…",
        )
        await self._reset_client()
        self.completed.set()

    async def _reset_client(self) -> None:
        if self.user_client:
            try:
                await self.user_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.user_client = None
        self.phone_code_hash = None

    def _reset_fields(self) -> None:
        self.state = "idle"
        self.phone = None
        self.phone_code_hash = None

    async def run(self) -> AuthConfig:
        application = (
            Application.builder()
            .token(self.bot_token)
            .concurrent_updates(False)
            .build()
        )
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("setup", self.start))
        application.add_handler(CommandHandler("cancel", self.cancel))
        application.add_handler(CallbackQueryHandler(self.callback, pattern=r"^setup:"))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        async with application:
            await application.start()
            if not application.updater:
                raise RuntimeError("Polling updater is unavailable")
            await application.updater.start_polling(drop_pending_updates=False)
            try:
                try:
                    await application.bot.send_message(
                        self.owner_id,
                        "🟢 Join Manager V4 اجرا شد. برای اتصال اکانت مدیر /start را بزن.",
                    )
                except Exception:  # noqa: BLE001
                    log.warning("Could not send bootstrap startup message")
                await self.completed.wait()
            finally:
                await application.updater.stop()
                await application.stop()

        if not self.result:
            raise RuntimeError("Setup completed without an auth config")
        return self.result


async def ensure_auth_config(
    *,
    bot_token: str,
    owner_id: int,
    auth_file: Path,
    persist_to_git: bool,
    force_setup: bool = False,
) -> tuple[AuthConfig, AuthVault]:
    vault = AuthVault(auth_file, bot_token, owner_id, persist_to_git)
    if not force_setup and vault.exists():
        try:
            return vault.load(), vault
        except RuntimeError:
            log.exception("Encrypted auth file could not be loaded; starting setup")
            vault.delete_local()
    wizard = SetupWizard(bot_token, owner_id, vault)
    return await wizard.run(), vault

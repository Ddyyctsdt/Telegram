from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import qrcode
from cryptography.fernet import Fernet, InvalidToken
from opentele2.api import API
from opentele2.tl import TelegramClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon.errors import FloodWaitError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

log = logging.getLogger("telegram-join-manager.bootstrap")
AUTH_VERSION = 5
API_TEMPLATE = "TelegramDesktop"


def build_user_api(owner_id: int):
    """Keep the same official-client fingerprint used by V4 for session stability."""
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
    connected_user_id: int | None = None
    connected_name: str | None = None
    connected_username: str | None = None
    api_template: str = API_TEMPLATE
    version: int = AUTH_VERSION


class AuthVault:
    """Encrypt the user MTProto session before persisting it in a private repository."""

    def __init__(self, path: Path, bot_token: str, owner_id: int, persist_to_git: bool) -> None:
        self.path = path
        self.root = path.resolve().parents[1]
        self.persist_to_git = persist_to_git
        seed = f"telegram-join-manager-v5\0{owner_id}\0{bot_token}".encode("utf-8")
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

        if int(raw.get("version", 0)) != AUTH_VERSION:
            raise RuntimeError("AUTH_VERSION_MISMATCH")
        session = str(raw.get("user_session", "")).strip()
        if not session:
            raise RuntimeError("EMPTY_USER_SESSION")
        return AuthConfig(
            user_session=session,
            connected_user_id=(
                int(raw["connected_user_id"]) if raw.get("connected_user_id") is not None else None
            ),
            connected_name=(str(raw.get("connected_name")) if raw.get("connected_name") else None),
            connected_username=(
                str(raw.get("connected_username")) if raw.get("connected_username") else None
            ),
            api_template=str(raw.get("api_template", API_TEMPLATE)),
            version=AUTH_VERSION,
        )

    async def save(self, config: AuthConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(config), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = self.fernet.encrypt(payload)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        tmp.replace(self.path)
        if self.persist_to_git and os.getenv("GITHUB_ACTIONS") == "true":
            await asyncio.to_thread(self._git_commit, "Save encrypted Telegram QR authorization")

    def delete_local(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            log.exception("Could not delete auth vault")

    async def clear(self) -> None:
        existed = self.path.exists()
        self.delete_local()
        if existed and self.persist_to_git and os.getenv("GITHUB_ACTIONS") == "true":
            await asyncio.to_thread(self._git_commit_deletion, "Remove encrypted Telegram authorization")

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

    def _git_commit_deletion(self, message: str) -> None:
        try:
            relative = str(self.path.relative_to(self.root))
            subprocess.run(["git", "add", "-A", relative], cwd=self.root, check=True)
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
            log.exception("Could not persist auth deletion to git")


class SetupWizard:
    QR_WAIT_SECONDS = 24

    def __init__(self, bot_token: str, owner_id: int, vault: AuthVault) -> None:
        self.bot_token = bot_token
        self.owner_id = owner_id
        self.vault = vault
        self.state = "idle"
        self.user_client: TelegramClient | None = None
        self.completed = asyncio.Event()
        self.result: AuthConfig | None = None
        self.qr_task: asyncio.Task[Any] | None = None
        self.qr_message_id: int | None = None

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
            [[InlineKeyboardButton("📲 اتصال اکانت با QR", callback_data="setup:qr")]]
        )
        text = (
            "<b>راه‌اندازی Telegram Join Manager V6</b>\n\n"
            "برای اتصال اکانت دیگر Login Code نمی‌گیریم. QR رسمی Telegram ساخته می‌شود و تو آن را "
            "از یک Telegram که از قبل وارد اکانت موردنظر است تأیید می‌کنی.\n\n"
            "<b>OWNER_ID فقط صاحب پنل است.</b> اکانتی که با QR متصل می‌کنی می‌تواند یک اکانت دیگر باشد.\n\n"
            "اگر حساب 2FA داشته باشد، بعد از تأیید QR فقط رمز دومرحله‌ای درخواست می‌شود."
        )
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            return
        await self._cancel_qr_task()
        await self._reset_client()
        self.state = "idle"
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
        if query.data == "setup:qr":
            await self._cancel_qr_task()
            await self._reset_client()
            self.state = "qr"
            await query.edit_message_text(
                "⏳ در حال ساخت QR ورود رسمی Telegram…\n\n"
                "QR حدود چند ده ثانیه اعتبار دارد و در صورت انقضا خودکار تازه می‌شود."
            )
            self.qr_task = asyncio.create_task(self._qr_login_flow(context))
        elif query.data == "setup:cancel":
            await self._cancel_qr_task()
            await self._reset_client()
            self.state = "idle"
            try:
                await query.edit_message_caption(caption="❌ اتصال لغو شد. /start را بزن.")
            except BadRequest:
                try:
                    await query.edit_message_text("❌ اتصال لغو شد. /start را بزن.")
                except BadRequest:
                    pass

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_owner(update):
            await self.reject_non_owner(update)
            return
        text = (update.effective_message.text or "").strip()
        if self.state != "password":
            await update.effective_message.reply_text(
                "برای اتصال اکانت از QR استفاده کن. /start را بزن."
            )
            return

        await self.safe_delete(update)
        if not text:
            await context.bot.send_message(self.owner_id, "رمز خالی است. دوباره بفرست.")
            return
        await self._sign_in_password(context, text)

    async def _new_user_client(self) -> TelegramClient:
        api = build_user_api(self.owner_id)
        client = TelegramClient(StringSession(), api=api)
        await client.connect()
        return client

    @staticmethod
    def _qr_bytes(url: str) -> io.BytesIO:
        image = qrcode.make(url)
        buffer = io.BytesIO()
        buffer.name = "telegram-login-qr.png"
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer

    async def _send_qr(self, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
        if self.qr_message_id:
            try:
                await context.bot.delete_message(self.owner_id, self.qr_message_id)
            except Exception:  # noqa: BLE001
                pass
        caption = (
            "🔐 <b>اتصال اکانت Telegram</b>\n\n"
            "روش ۱: اگر دستگاه دوم داری، در Telegram اکانت موردنظر برو به:\n"
            "<code>Settings → Devices → Link Desktop Device</code>\n"
            "و این QR را اسکن کن.\n\n"
            "روش ۲: دکمه «باز کردن لینک ورود» را امتحان کن؛ روی بعضی کلاینت‌ها همان گوشی هم می‌تواند توکن را تأیید کند.\n\n"
            "⏳ اگر QR منقضی شود خودکار QR تازه می‌فرستم."
        )
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📲 باز کردن لینک ورود", url=url)],
                [InlineKeyboardButton("❌ لغو", callback_data="setup:cancel")],
            ]
        )
        try:
            msg = await context.bot.send_photo(
                chat_id=self.owner_id,
                photo=self._qr_bytes(url),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except BadRequest:
            # Some Bot API/client combinations may reject a tg:// URL button.
            msg = await context.bot.send_photo(
                chat_id=self.owner_id,
                photo=self._qr_bytes(url),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ لغو", callback_data="setup:cancel")]]
                ),
            )
        self.qr_message_id = msg.message_id

    async def _qr_login_flow(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            self.user_client = await self._new_user_client()
            qr = await self.user_client.qr_login()
            while self.state == "qr":
                await self._send_qr(context, qr.url)
                try:
                    await qr.wait(timeout=self.QR_WAIT_SECONDS)
                    await self._finish(context)
                    return
                except asyncio.TimeoutError:
                    await qr.recreate()
                    continue
                except SessionPasswordNeededError:
                    self.state = "password"
                    await context.bot.send_message(
                        self.owner_id,
                        "🔐 QR تأیید شد، ولی روی این حساب تأیید دومرحله‌ای فعال است.\n\n"
                        "رمز 2FA را به‌صورت متن بفرست. پیام بعد از دریافت تا حد امکان حذف می‌شود.\n"
                        "برای لغو: /cancel",
                    )
                    return
                except FloodWaitError as exc:
                    await context.bot.send_message(
                        self.owner_id,
                        f"Telegram محدودیت موقت داده است: {exc.seconds} ثانیه. /start را بعداً دوباره بزن.",
                    )
                    self.state = "idle"
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("QR login failed")
            self.state = "idle"
            await context.bot.send_message(
                self.owner_id,
                f"❌ ساخت/تأیید QR ناموفق بود: <code>{type(exc).__name__}</code>\n/start را دوباره بزن.",
                parse_mode=ParseMode.HTML,
            )
        finally:
            self.qr_task = None

    async def _sign_in_password(self, context: ContextTypes.DEFAULT_TYPE, password: str) -> None:
        if not self.user_client:
            self.state = "idle"
            await context.bot.send_message(self.owner_id, "نشست QR از بین رفته؛ /start را دوباره بزن.")
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
        first = getattr(me, "first_name", None) or ""
        last = getattr(me, "last_name", None) or ""
        display_name = (first + " " + last).strip() or "Telegram User"
        username = getattr(me, "username", None)
        session = StringSession.save(self.user_client.session)
        config = AuthConfig(
            user_session=session,
            connected_user_id=int(me.id),
            connected_name=display_name,
            connected_username=username,
        )
        await self.vault.save(config)
        self.result = config
        self.state = "done"
        await context.bot.send_message(
            self.owner_id,
            "✅ <b>اکانت با موفقیت متصل شد.</b>\n\n"
            f"👤 {display_name}\n"
            f"🆔 <code>{int(me.id)}</code>\n"
            + (f"🔗 @{username}\n" if username else "")
            + "\nSession رمزگذاری و ذخیره شد. در حال انتقال به پنل اصلی…",
            parse_mode=ParseMode.HTML,
        )
        await self._reset_client()
        self.completed.set()

    async def _cancel_qr_task(self) -> None:
        task = self.qr_task
        self.qr_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _reset_client(self) -> None:
        if self.user_client:
            try:
                await self.user_client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.user_client = None

    async def run(self) -> AuthConfig:
        application = Application.builder().token(self.bot_token).concurrent_updates(False).build()
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
                        "🟢 Join Manager V6 اجرا شد. برای اتصال اکانت با QR، /start را بزن.",
                    )
                except Exception:  # noqa: BLE001
                    log.warning("Could not send bootstrap startup message")
                await self.completed.wait()
            finally:
                await self._cancel_qr_task()
                await self._reset_client()
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
            log.exception("Encrypted auth file could not be loaded; starting QR setup")
            vault.delete_local()
    wizard = SetupWizard(bot_token, owner_id, vault)
    return await wizard.run(), vault

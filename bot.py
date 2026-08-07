from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import random
import re
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytesseract
from PIL import Image, ImageFilter, ImageOps
from opentele2.tl import TelegramClient
from telethon import Button, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

from bootstrap import AuthConfig, AuthVault, build_bot_api, build_user_api, ensure_auth_config

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "state.json"
AUTH_FILE = ROOT / "data" / "auth.enc"
NUMBER_RE = re.compile(r"^-?\d+$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("telegram-join-manager.v6")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def epoch_datetime() -> datetime:
    return datetime.fromtimestamp(0, tz=timezone.utc)


def unix_timestamp(value: Any | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def to_telegram_datetime(timestamp: int | None) -> datetime | int | None:
    if timestamp is None:
        return None
    if int(timestamp) == 0:
        # MTProto clients commonly use 0 to clear an expiration date.
        return 0
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def fmt_num(value: int | None) -> str:
    return f"{int(value or 0):,}"


def compact_title(value: str | None, fallback: str, limit: int = 34) -> str:
    text = (value or fallback).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_expiry(timestamp: int | None) -> str:
    if not timestamp:
        return "بدون انقضا"
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    remaining = timestamp - int(time.time())
    if remaining <= 0:
        return "منقضی‌شده"
    if remaining < 3600:
        return f"حدود {max(1, remaining // 60)} دقیقه دیگر"
    if remaining < 86400:
        return f"حدود {max(1, remaining // 3600)} ساعت دیگر"
    return f"{max(1, remaining // 86400)} روز دیگر • {dt.strftime('%Y-%m-%d %H:%M')} UTC"


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(value: str) -> str:
    return value.translate(PERSIAN_DIGITS)


def ocr_digits_from_bytes(blob: bytes) -> int | None:
    """OCR ordinary numeric admin inputs only; never Telegram login secrets."""
    with Image.open(io.BytesIO(blob)) as source:
        image = ImageOps.exif_transpose(source).convert("L")
        image = ImageOps.autocontrast(image)
        if image.width < 1400:
            scale = max(2, min(4, 1400 // max(1, image.width)))
            image = image.resize((image.width * scale, image.height * scale))
        image = image.filter(ImageFilter.SHARPEN)
        image = image.point(lambda px: 255 if px > 165 else 0)
        config = "--psm 6 -c tessedit_char_whitelist=0123456789۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩"
        try:
            raw = pytesseract.image_to_string(image, lang="eng+fas", config=config)
        except pytesseract.TesseractError:
            raw = pytesseract.image_to_string(image, lang="eng", config=config)
    groups = re.findall(r"\d+", normalize_digits(raw))
    if not groups:
        return None
    try:
        return int(max(groups, key=len))
    except ValueError:
        return None


def marked_peer_id(entity_or_peer: Any) -> int:
    return int(utils.get_peer_id(entity_or_peer))


class OwnerRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    user_session: str
    connected_user_id: int | None
    connected_name: str | None
    connected_username: str | None
    bot_token: str
    owner_id: int
    approval_concurrency: int
    auto_scan_seconds: int
    auto_approve_batch: int
    report_interval_seconds: int
    run_seconds: int
    persist_to_git: bool

    @classmethod
    def load(cls, auth: AuthConfig) -> "Settings":
        required = ["BOT_TOKEN", "OWNER_ID"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
        return cls(
            user_session=auth.user_session.strip(),
            connected_user_id=auth.connected_user_id,
            connected_name=auth.connected_name,
            connected_username=auth.connected_username,
            bot_token=os.environ["BOT_TOKEN"].strip(),
            owner_id=int(os.environ["OWNER_ID"]),
            approval_concurrency=max(1, min(40, int(os.getenv("APPROVAL_CONCURRENCY", "15")))),
            auto_scan_seconds=max(10, int(os.getenv("AUTO_SCAN_SECONDS", "20"))),
            auto_approve_batch=max(1, min(500, int(os.getenv("AUTO_APPROVE_BATCH", "100")))),
            report_interval_seconds=max(30, int(os.getenv("REPORT_INTERVAL_SECONDS", "60"))),
            run_seconds=max(60, int(os.getenv("RUN_SECONDS", "20000"))),
            persist_to_git=os.getenv("PERSIST_TO_GIT", "true").lower() in {"1", "true", "yes", "on"},
        )


class Store:
    def __init__(self, path: Path, persist_to_git: bool) -> None:
        self.path = path
        self.persist_to_git = persist_to_git
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = self._empty_data()
        self.load()

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": 3,
            "active_channel_id": None,
            "channels": [],
            "policies": {},
        }

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save_local()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root must be an object")
            raw["version"] = 3
            raw.setdefault("active_channel_id", None)
            raw.setdefault("channels", [])
            raw.setdefault("policies", {})
            for policy in raw["policies"].values():
                policy.setdefault("report_enabled", False)
            self.data = raw
        except (OSError, ValueError, json.JSONDecodeError):
            log.exception("Could not read state file; starting with empty state")
            self.data = self._empty_data()

    def save_local(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def save(self, commit_message: str) -> None:
        async with self.lock:
            self.save_local()
            if self.persist_to_git and os.getenv("GITHUB_ACTIONS") == "true":
                await asyncio.to_thread(self._git_commit, commit_message)

    def _git_commit(self, message: str) -> None:
        try:
            relative = str(self.path.relative_to(ROOT))
            subprocess.run(["git", "add", relative], cwd=ROOT, check=True)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False)
            if diff.returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True)
            subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=ROOT, check=True)
            subprocess.run(["git", "push"], cwd=ROOT, check=True)
        except subprocess.CalledProcessError:
            log.exception("Could not persist state to git; local state remains updated")

    @property
    def active_channel_id(self) -> int | None:
        value = self.data.get("active_channel_id")
        return int(value) if value is not None else None

    def channels(self) -> list[dict[str, Any]]:
        return list(self.data.get("channels", []))

    def get_channel(self, channel_id: int) -> dict[str, Any] | None:
        for item in self.channels():
            if int(item.get("id")) == int(channel_id):
                return item
        return None

    def get_channel_by_key(self, key: str) -> dict[str, Any] | None:
        for item in self.channels():
            if short_hash(str(item.get("id")), 10) == key:
                return item
        return None

    async def upsert_channel(self, channel: dict[str, Any], make_active: bool = True) -> None:
        channel_id = int(channel["id"])
        found = self.get_channel(channel_id)
        if found:
            found.update(channel)
        else:
            self.data.setdefault("channels", []).append(channel)
        if make_active:
            self.data["active_channel_id"] = channel_id
        await self.save("Register Telegram channel")

    async def set_active_channel(self, channel_id: int) -> None:
        if not self.get_channel(channel_id):
            raise ValueError("Channel is not registered")
        self.data["active_channel_id"] = int(channel_id)
        await self.save("Change active Telegram channel")

    async def remove_channel(self, channel_id: int) -> bool:
        before = len(self.data.get("channels", []))
        self.data["channels"] = [
            item for item in self.data.get("channels", []) if int(item.get("id")) != int(channel_id)
        ]
        if len(self.data["channels"]) == before:
            return False
        if self.active_channel_id == int(channel_id):
            items = self.channels()
            self.data["active_channel_id"] = int(items[0]["id"]) if items else None
        policies = self.data.setdefault("policies", {})
        for key in list(policies):
            if int(policies[key].get("channel_id", 0)) == int(channel_id):
                policies.pop(key, None)
        await self.save("Remove Telegram channel")
        return True

    @staticmethod
    def policy_key(channel_id: int, link: str) -> str:
        return f"{int(channel_id)}:{short_hash(link, 16)}"

    def get_policy(self, channel_id: int, link: str) -> dict[str, Any] | None:
        return self.data.setdefault("policies", {}).get(self.policy_key(channel_id, link))

    def enabled_policies(self) -> list[dict[str, Any]]:
        return [
            item for item in self.data.setdefault("policies", {}).values()
            if bool(item.get("enabled"))
        ]

    async def set_policy(
        self,
        channel_id: int,
        link: str,
        title: str,
        max_approvals: int,
        initial_usage: int,
        report_enabled: bool | None = None,
    ) -> dict[str, Any]:
        key = self.policy_key(channel_id, link)
        policy = self.data.setdefault("policies", {}).get(key)
        if policy is None:
            policy = {
                "channel_id": int(channel_id),
                "link": link,
                "title": title,
                "max_approvals": int(max_approvals),
                "approved_by_bot": 0,
                "initial_usage": int(initial_usage),
                "enabled": int(max_approvals) > 0,
                "report_enabled": bool(report_enabled),
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "last_error": None,
            }
            self.data["policies"][key] = policy
        else:
            policy["title"] = title
            policy["max_approvals"] = int(max_approvals)
            policy["enabled"] = int(max_approvals) > 0
            if report_enabled is not None:
                policy["report_enabled"] = bool(report_enabled)
            policy["updated_at"] = utc_now_iso()
        await self.save("Update invite automation settings")
        return policy

    async def set_report_enabled(
        self,
        channel_id: int,
        link: str,
        title: str,
        initial_usage: int,
        enabled: bool,
    ) -> dict[str, Any]:
        existing = self.get_policy(channel_id, link)
        if existing:
            existing["report_enabled"] = bool(enabled)
            existing["title"] = title
            existing["updated_at"] = utc_now_iso()
            await self.save("Update invite minute reports")
            return existing
        return await self.set_policy(
            channel_id,
            link,
            title,
            max_approvals=0,
            initial_usage=initial_usage,
            report_enabled=enabled,
        )

    async def update_policy_progress(
        self,
        channel_id: int,
        link: str,
        success_delta: int = 0,
        enabled: bool | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        policy = self.get_policy(channel_id, link)
        if not policy:
            return None
        if success_delta:
            policy["approved_by_bot"] = int(policy.get("approved_by_bot", 0)) + int(success_delta)
        if enabled is not None:
            policy["enabled"] = bool(enabled)
        policy["last_error"] = last_error
        policy["updated_at"] = utc_now_iso()
        await self.save("Update automatic approval progress")
        return policy

    async def disable_policy(self, channel_id: int, link: str) -> bool:
        policy = self.get_policy(channel_id, link)
        if not policy:
            return False
        policy["enabled"] = False
        policy["updated_at"] = utc_now_iso()
        await self.save("Disable automatic approval policy")
        return True


@dataclass
class ChannelInfo:
    id: int
    title: str
    username: str | None
    entity: Any
    input_peer: Any

    @property
    def is_creator(self) -> bool:
        return bool(getattr(self.entity, "creator", False))

    @property
    def admin_rights(self) -> Any | None:
        return getattr(self.entity, "admin_rights", None)

    @property
    def can_invite_users(self) -> bool:
        return bool(self.is_creator or (self.admin_rights and getattr(self.admin_rights, "invite_users", False)))

    def as_store_item(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "title": self.title,
            "username": self.username,
            "is_creator": self.is_creator,
            "can_invite_users": self.can_invite_users,
            "added_at": utc_now_iso(),
        }


@dataclass
class ChannelOverview:
    members: int
    pending: int
    is_creator: bool
    can_invite_users: bool


@dataclass
class InviteInfo:
    link: str
    key: str
    title: str
    admin_id: int
    admin_name: str
    request_needed: bool
    revoked: bool
    permanent: bool
    usage: int
    usage_limit: int | None
    requested_hint: int
    expire_date: int | None
    created_date: int | None
    pending: int | None = None

    @property
    def expired(self) -> bool:
        return bool(self.expire_date and self.expire_date <= int(time.time()))

    @property
    def active(self) -> bool:
        return not self.revoked and not self.expired


ProgressCallback = Callable[[int, int, int], Awaitable[None]]
ScanProgressCallback = Callable[[int, int], Awaitable[None]]


class TelegramJoinService:
    def __init__(self, user_client: TelegramClient, bot_client: TelegramClient, store: Store, approval_concurrency: int) -> None:
        self.user = user_client
        self.bot = bot_client
        self.store = store
        self.approval_concurrency = approval_concurrency
        self.channel_cache: dict[int, ChannelInfo] = {}
        self.approval_locks: dict[int, asyncio.Lock] = {}
        self._self_user_id: int | None = None

    async def self_user_id(self) -> int:
        if self._self_user_id is None:
            me = await self.user.get_me()
            self._self_user_id = int(me.id)
        return self._self_user_id

    async def resolve_channel(self, ref: int | str) -> ChannelInfo:
        if isinstance(ref, str) and not NUMBER_RE.fullmatch(ref.strip()):
            entity = await self.user.get_entity(ref.strip())
            return await self._channel_info_from_entity(entity)
        target = int(ref)
        if target in self.channel_cache:
            return self.channel_cache[target]
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, types.Channel):
                continue
            dialog_marked = marked_peer_id(entity)
            if target in {dialog_marked, int(entity.id), int(dialog.id)}:
                info = await self._channel_info_from_entity(entity, dialog.input_entity)
                self.channel_cache[info.id] = info
                return info
        raise ValueError("کانال در اکانت متصل پیدا نشد. اکانت QR شده باید داخل کانال باشد.")

    async def _channel_info_from_entity(self, entity: Any, input_peer: Any | None = None) -> ChannelInfo:
        if not isinstance(entity, types.Channel):
            raise ValueError("شناسه مربوط به کانال یا سوپرگروه نیست.")
        peer = input_peer or await self.user.get_input_entity(entity)
        info = ChannelInfo(
            id=marked_peer_id(entity),
            title=str(getattr(entity, "title", None) or "کانال بدون نام"),
            username=getattr(entity, "username", None),
            entity=entity,
            input_peer=peer,
        )
        self.channel_cache[info.id] = info
        return info

    async def _refresh_channel(self, channel: ChannelInfo) -> tuple[ChannelInfo, Any]:
        result = await self.user(functions.channels.GetFullChannelRequest(channel=channel.input_peer))
        fresh = next(
            (chat for chat in getattr(result, "chats", []) if isinstance(chat, types.Channel) and int(chat.id) == int(channel.entity.id)),
            None,
        )
        if fresh is not None:
            channel = await self._channel_info_from_entity(fresh)
        return channel, result.full_chat

    async def verify_admin_access(self, channel: ChannelInfo) -> ChannelInfo:
        try:
            channel, _ = await self._refresh_channel(channel)
        except RPCError:
            pass
        log.info(
            "Channel admin check channel=%s user=%s creator=%s invite_users=%s rights_present=%s",
            channel.id,
            await self.self_user_id(),
            channel.is_creator,
            channel.can_invite_users,
            bool(channel.admin_rights),
        )
        if not bool(channel.is_creator or channel.admin_rights):
            raise ValueError("اکانت متصل از دید Telegram ادمین این کانال نیست.")
        if not channel.can_invite_users:
            raise ValueError("اکانت متصل ادمین است ولی دسترسی Invite Users / Add Subscribers ندارد.")
        return channel

    async def register_channel(self, ref: int | str) -> ChannelInfo:
        channel = await self.resolve_channel(ref)
        channel = await self.verify_admin_access(channel)
        await self.store.upsert_channel(channel.as_store_item(), make_active=True)
        return channel

    async def scan_manageable_channels(self) -> list[ChannelInfo]:
        found: dict[int, ChannelInfo] = {}
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, types.Channel):
                continue
            if not bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None)):
                continue
            rights = getattr(entity, "admin_rights", None)
            if not bool(getattr(entity, "creator", False) or (rights and getattr(rights, "invite_users", False))):
                continue
            try:
                info = await self._channel_info_from_entity(entity, dialog.input_entity)
            except ValueError:
                continue
            found[info.id] = info
        return sorted(found.values(), key=lambda item: item.title.casefold())

    async def channel_overview(self, channel_id: int) -> ChannelOverview:
        channel = await self.resolve_channel(channel_id)
        channel, full = await self._refresh_channel(channel)
        members = int(getattr(full, "participants_count", 0) or 0)
        pending = int(getattr(full, "requests_pending", 0) or 0)
        await self.store.upsert_channel(channel.as_store_item(), make_active=False)
        return ChannelOverview(
            members=members,
            pending=pending,
            is_creator=channel.is_creator,
            can_invite_users=channel.can_invite_users,
        )

    async def _invite_admin_users(self, channel: ChannelInfo) -> list[tuple[Any, str, int]]:
        me = await self.user.get_me()
        admins: dict[int, tuple[Any, str, int]] = {}

        def add_user(user: Any, fallback_name: str = "ادمین") -> None:
            try:
                input_user = utils.get_input_user(user)
            except TypeError:
                return
            uid = int(user.id)
            name = " ".join(
                part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part
            ).strip() or getattr(user, "username", None) or fallback_name or str(uid)
            admins[uid] = (input_user, name, uid)

        add_user(me, "اکانت متصل")
        # Telegram only guarantees access to other admins' invite links for the channel owner.
        if not channel.is_creator:
            return list(admins.values())
        try:
            result = await self.user(
                functions.channels.GetParticipantsRequest(
                    channel=channel.entity,
                    filter=types.ChannelParticipantsAdmins(),
                    offset=0,
                    limit=200,
                    hash=0,
                )
            )
            for user in getattr(result, "users", []):
                add_user(user)
        except RPCError as exc:
            log.warning("Could not enumerate admins for channel %s: %s", channel.id, type(exc).__name__)
        return list(admins.values())

    @staticmethod
    def _invite_from_exported(invite: Any, admin_name: str = "اکانت مدیر") -> InviteInfo:
        return InviteInfo(
            link=str(invite.link),
            key=short_hash(str(invite.link)),
            title=str(getattr(invite, "title", None) or "لینک بدون عنوان"),
            admin_id=int(getattr(invite, "admin_id", 0) or 0),
            admin_name=admin_name,
            request_needed=bool(getattr(invite, "request_needed", False)),
            revoked=bool(getattr(invite, "revoked", False)),
            permanent=bool(getattr(invite, "permanent", False)),
            usage=int(getattr(invite, "usage", 0) or 0),
            usage_limit=(int(getattr(invite, "usage_limit")) if getattr(invite, "usage_limit", None) is not None else None),
            requested_hint=int(getattr(invite, "requested", 0) or 0),
            expire_date=unix_timestamp(getattr(invite, "expire_date", None)),
            created_date=unix_timestamp(getattr(invite, "date", None)),
            pending=None,
        )

    async def list_invites(self, channel_id: int) -> list[InviteInfo]:
        channel = await self.resolve_channel(channel_id)
        channel = await self.verify_admin_access(channel)
        admin_users = await self._invite_admin_users(channel)
        all_invites: dict[str, InviteInfo] = {}
        for admin_input, admin_name, admin_id in admin_users:
            offset_date: datetime | None = None
            offset_link: str | None = None
            seen_offsets: set[tuple[int | None, str | None]] = set()
            while True:
                try:
                    response = await self.user(
                        functions.messages.GetExportedChatInvitesRequest(
                            peer=channel.input_peer,
                            admin_id=admin_input,
                            revoked=False,
                            offset_date=offset_date,
                            offset_link=offset_link,
                            limit=100,
                        )
                    )
                except RPCError as exc:
                    log.warning(
                        "Could not fetch invite links for admin %s in channel %s: %s",
                        admin_id,
                        channel.id,
                        type(exc).__name__,
                    )
                    break
                page = list(response.invites)
                for invite in page:
                    if isinstance(invite, types.ChatInviteExported):
                        all_invites[str(invite.link)] = self._invite_from_exported(invite, admin_name)
                if len(page) < 100:
                    break
                last = page[-1]
                next_key = (unix_timestamp(getattr(last, "date", None)), str(getattr(last, "link", "")))
                if next_key in seen_offsets:
                    break
                seen_offsets.add(next_key)
                offset_date = getattr(last, "date", None)
                offset_link = str(getattr(last, "link", ""))
        return sorted(all_invites.values(), key=lambda item: (not item.active, not item.request_needed, item.title.casefold()))

    async def pending_count(self, channel_id: int, link: str) -> int:
        channel = await self.resolve_channel(channel_id)
        result = await self.user(
            functions.messages.GetChatInviteImportersRequest(
                peer=channel.input_peer,
                requested=True,
                link=link,
                q=None,
                offset_date=epoch_datetime(),
                offset_user=types.InputUserEmpty(),
                limit=1,
            )
        )
        return int(result.count)

    async def enrich_pending_counts(self, channel_id: int, invites: list[InviteInfo]) -> list[InviteInfo]:
        semaphore = asyncio.Semaphore(5)

        async def fill(invite: InviteInfo) -> None:
            if not invite.active or not invite.request_needed:
                invite.pending = 0
                return
            async with semaphore:
                try:
                    invite.pending = await self.pending_count(channel_id, invite.link)
                except RPCError:
                    invite.pending = invite.requested_hint

        await asyncio.gather(*(fill(invite) for invite in invites))
        return invites

    async def find_invite(self, channel_id: int, invite_key: str) -> InviteInfo | None:
        invites = await self.list_invites(channel_id)
        for invite in invites:
            if invite.key == invite_key:
                if invite.active and invite.request_needed:
                    try:
                        invite.pending = await self.pending_count(channel_id, invite.link)
                    except RPCError:
                        invite.pending = invite.requested_hint
                else:
                    invite.pending = 0
                return invite
        return None

    async def create_invite(
        self,
        channel_id: int,
        title: str,
        request_needed: bool,
        expire_at: int | None,
        usage_limit: int | None = None,
    ) -> InviteInfo:
        channel = await self.resolve_channel(channel_id)
        if request_needed:
            usage_limit = None
        result = await self.user(
            functions.messages.ExportChatInviteRequest(
                peer=channel.input_peer,
                request_needed=request_needed,
                expire_date=to_telegram_datetime(expire_at),
                usage_limit=usage_limit,
                title=title[:32],
            )
        )
        if not isinstance(result, types.ChatInviteExported):
            raise RuntimeError("تلگرام لینک خروجی قابل استفاده برنگرداند.")
        info = self._invite_from_exported(result, "اکانت متصل")
        info.pending = 0
        return info

    async def edit_invite_title(self, channel_id: int, link: str, title: str) -> InviteInfo:
        channel = await self.resolve_channel(channel_id)
        response = await self.user(
            functions.messages.EditExportedChatInviteRequest(
                peer=channel.input_peer,
                link=link,
                title=title[:32],
            )
        )
        invite = getattr(response, "invite", None)
        if not isinstance(invite, types.ChatInviteExported):
            raise RuntimeError("پاسخ ویرایش لینک قابل خواندن نبود.")
        return self._invite_from_exported(invite, "اکانت متصل")

    async def edit_invite_expiry(self, channel_id: int, link: str, expire_at: int | None) -> InviteInfo:
        channel = await self.resolve_channel(channel_id)
        expire_value: Any = 0 if expire_at is None else to_telegram_datetime(expire_at)
        response = await self.user(
            functions.messages.EditExportedChatInviteRequest(
                peer=channel.input_peer,
                link=link,
                expire_date=expire_value,
            )
        )
        invite = getattr(response, "invite", None)
        if not isinstance(invite, types.ChatInviteExported):
            raise RuntimeError("پاسخ ویرایش لینک قابل خواندن نبود.")
        return self._invite_from_exported(invite, "اکانت متصل")

    async def revoke_invite(self, channel_id: int, link: str) -> None:
        channel = await self.resolve_channel(channel_id)
        await self.user(
            functions.messages.EditExportedChatInviteRequest(
                peer=channel.input_peer,
                link=link,
                revoked=True,
            )
        )

    async def _require_owner_for_global_queue(self, channel_id: int) -> ChannelInfo:
        channel = await self.resolve_channel(channel_id)
        try:
            channel, _ = await self._refresh_channel(channel)
        except RPCError:
            pass
        if not channel.is_creator:
            raise OwnerRequiredError(
                "برای گرفتن فهرست کامل درخواست‌های همه لینک‌های کانال، Session باید متعلق به Owner کانال باشد."
            )
        return channel

    def _approval_lock(self, channel_id: int) -> asyncio.Lock:
        if int(channel_id) not in self.approval_locks:
            self.approval_locks[int(channel_id)] = asyncio.Lock()
        return self.approval_locks[int(channel_id)]

    async def sample_pending_users(
        self,
        channel_id: int,
        amount: int,
        link: str | None,
        scan_progress: ScanProgressCallback | None = None,
    ) -> tuple[list[Any], int, int]:
        if amount <= 0:
            raise ValueError("تعداد باید بیشتر از صفر باشد.")
        if link is None:
            channel = await self._require_owner_for_global_queue(channel_id)
        else:
            channel = await self.resolve_channel(channel_id)

        offset_date = epoch_datetime()
        offset_user: Any = types.InputUserEmpty()
        page_size = 100
        rng = random.SystemRandom()
        reservoir: list[Any] = []
        seen_ids: set[int] = set()
        seen_offsets: set[tuple[int, int]] = set()
        seen = 0
        reported_total = 0
        last_progress = 0

        while True:
            try:
                result = await self.user(
                    functions.messages.GetChatInviteImportersRequest(
                        peer=channel.input_peer,
                        requested=True,
                        link=link,
                        q=None,
                        offset_date=offset_date,
                        offset_user=offset_user,
                        limit=page_size,
                    )
                )
            except FloodWaitError as exc:
                log.warning("Pending queue scan FloodWait %s seconds", exc.seconds)
                await asyncio.sleep(exc.seconds + 1)
                continue
            reported_total = max(reported_total, int(result.count or 0))
            users_by_id = {int(user.id): user for user in result.users}
            for importer in result.importers:
                uid = int(importer.user_id)
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                user = users_by_id.get(uid)
                if user is None:
                    continue
                try:
                    input_user = utils.get_input_user(user)
                except TypeError:
                    continue
                seen += 1
                if len(reservoir) < amount:
                    reservoir.append(input_user)
                else:
                    index = rng.randrange(seen)
                    if index < amount:
                        reservoir[index] = input_user
            if scan_progress and (seen - last_progress >= 500 or len(result.importers) < page_size):
                last_progress = seen
                await scan_progress(seen, reported_total)
            if len(result.importers) < page_size:
                break
            last = result.importers[-1]
            last_user = users_by_id.get(int(last.user_id))
            if last_user is None:
                break
            next_offset = (int(last.date.timestamp()), int(last.user_id))
            if next_offset in seen_offsets:
                log.warning("Pending queue pagination repeated offset %s; stopping to avoid an infinite loop", next_offset)
                break
            seen_offsets.add(next_offset)
            offset_date = last.date
            offset_user = utils.get_input_user(last_user)
            if reported_total and seen >= reported_total:
                break
        return reservoir, seen, reported_total

    async def approve_selected(
        self,
        channel_id: int,
        selected: list[Any],
        progress: ProgressCallback | None = None,
    ) -> tuple[int, int, list[str]]:
        channel = await self.resolve_channel(channel_id)
        semaphore = asyncio.Semaphore(self.approval_concurrency)
        counter_lock = asyncio.Lock()
        done = 0
        success = 0
        errors: list[str] = []

        async def approve_one(user: Any) -> None:
            nonlocal done, success
            ok = False
            async with semaphore:
                try:
                    await self.user(
                        functions.messages.HideChatJoinRequestRequest(
                            peer=channel.input_peer,
                            user_id=user,
                            approved=True,
                        )
                    )
                    ok = True
                except FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds + 1)
                    try:
                        await self.user(
                            functions.messages.HideChatJoinRequestRequest(
                                peer=channel.input_peer,
                                user_id=user,
                                approved=True,
                            )
                        )
                        ok = True
                    except RPCError as retry_exc:
                        errors.append(type(retry_exc).__name__)
                except RPCError as exc:
                    errors.append(type(exc).__name__)
            async with counter_lock:
                done += 1
                if ok:
                    success += 1
                if progress and (done == len(selected) or done % 25 == 0):
                    await progress(done, len(selected), success)

        await asyncio.gather(*(approve_one(user) for user in selected))
        return success, len(selected), errors

    async def approve_random(
        self,
        channel_id: int,
        amount: int,
        link: str | None,
        progress: ProgressCallback | None = None,
        scan_progress: ScanProgressCallback | None = None,
    ) -> tuple[int, int, int, list[str]]:
        async with self._approval_lock(channel_id):
            selected, seen, _ = await self.sample_pending_users(channel_id, amount, link, scan_progress)
            if not selected:
                return 0, 0, seen, []
            success, selected_count, errors = await self.approve_selected(channel_id, selected, progress)
            return success, selected_count, seen, errors

    async def approve_all(self, channel_id: int, link: str | None) -> None:
        async with self._approval_lock(channel_id):
            if link is None:
                channel = await self._require_owner_for_global_queue(channel_id)
            else:
                channel = await self.resolve_channel(channel_id)
            await self.user(
                functions.messages.HideAllChatJoinRequestsRequest(
                    peer=channel.input_peer,
                    approved=True,
                    link=link,
                )
            )


class ControlBot:
    PAGE_SIZE = 8

    def __init__(self, bot: TelegramClient, service: TelegramJoinService, store: Store, settings: Settings, vault: AuthVault) -> None:
        self.bot = bot
        self.service = service
        self.store = store
        self.settings = settings
        self.vault = vault
        self.reauth_requested = False
        self.states: dict[int, dict[str, Any]] = {}
        self.invite_cache: dict[tuple[int, str], InviteInfo] = {}
        self.auto_task: asyncio.Task[Any] | None = None
        self.report_task: asyncio.Task[Any] | None = None
        self.report_events: dict[tuple[int, str], int] = defaultdict(int)
        self.report_titles: dict[tuple[int, str], str] = {}
        self.report_lock = asyncio.Lock()

    async def start(self) -> None:
        self.bot.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
        self.bot.add_event_handler(self.on_raw_update, events.Raw())
        self.auto_task = asyncio.create_task(self.auto_approval_loop())
        self.report_task = asyncio.create_task(self.minute_report_loop())

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == self.settings.owner_id

    def set_state(self, user_id: int, name: str, **payload: Any) -> None:
        self.states[user_id] = {"name": name, **payload}

    def pop_state(self, user_id: int) -> dict[str, Any] | None:
        return self.states.pop(user_id, None)

    def active_channel(self) -> dict[str, Any] | None:
        cid = self.store.active_channel_id
        return self.store.get_channel(cid) if cid is not None else None

    @staticmethod
    def numeric_state(state: dict[str, Any] | None) -> bool:
        return bool(
            state and state.get("name") in {
                "global_approve_custom",
                "link_approve_custom",
                "create_expire_custom",
                "create_auto_custom",
                "create_usage_custom",
                "set_auto_limit",
                "edit_expire_custom",
            }
        )

    @staticmethod
    def message_has_image(message: Any) -> bool:
        if getattr(message, "photo", None) is not None:
            return True
        document = getattr(message, "document", None)
        mime = getattr(document, "mime_type", "") if document is not None else ""
        return isinstance(mime, str) and mime.startswith("image/")

    async def detect_number_from_image(self, event: events.NewMessage.Event) -> int | None:
        try:
            blob = await self.bot.download_media(event.message, file=bytes)
            if not blob:
                return None
            return await asyncio.to_thread(ocr_digits_from_bytes, blob)
        except Exception:
            log.exception("Numeric OCR failed")
            return None

    def onboarding_text(self) -> str:
        return (
            "🤖 **Telegram Join Manager V6**\n\n"
            "اکانت QR شده باید داخل کانال Admin و دارای دسترسی Invite Users باشد.\n"
            "برای دسترسی کامل به صف همه لینک‌ها و لینک‌های ساخته‌شده توسط سایر ادمین‌ها، اکانت متصل باید **Owner کانال** باشد."
        )

    def onboarding_buttons(self) -> list[list[Button]]:
        return [
            [Button.inline("🔎 شناسایی کانال‌های قابل مدیریت", b"scan_channels")],
            [Button.inline("🆔 ارسال آیدی کانال", b"ask_channel")],
            [Button.inline("ℹ️ راهنمای اتصال", b"setup_help")],
        ]

    def dashboard_buttons(self) -> list[list[Button]]:
        return [
            [Button.inline("⏳ مدیریت درخواست‌های کانال", b"channel_requests")],
            [Button.inline("🔗 مدیریت لینک‌ها", b"links"), Button.inline("➕ ساخت لینک", b"create_link")],
            [Button.inline("🔄 بروزرسانی", b"dashboard"), Button.inline("📺 کانال‌ها", b"channels")],
            [Button.inline("ℹ️ راهنما", b"help")],
            [Button.inline("🔄 تغییر اکانت متصل", b"reauth")],
        ]

    async def on_raw_update(self, update: Any) -> None:
        if not isinstance(update, types.UpdateBotChatInviteRequester):
            return
        try:
            channel_id = marked_peer_id(update.peer)
        except Exception:
            return
        if not self.store.get_channel(channel_id):
            return
        invite = getattr(update, "invite", None)
        link = getattr(invite, "link", None)
        if not link:
            return
        policy = self.store.get_policy(channel_id, str(link))
        if not policy or not bool(policy.get("report_enabled")):
            return
        key = (channel_id, str(link))
        async with self.report_lock:
            self.report_events[key] += 1
            self.report_titles[key] = str(getattr(invite, "title", None) or policy.get("title") or "لینک")

    async def on_message(self, event: events.NewMessage.Event) -> None:
        if not self.is_owner(event.sender_id):
            return
        text = (event.raw_text or "").strip()
        if text in {"/cancel", "لغو"}:
            self.pop_state(event.sender_id)
            await event.respond("عملیات لغو شد.", buttons=[[Button.inline("🏠 منو", b"home")]])
            return
        if text == "/reauth":
            self.pop_state(event.sender_id)
            await event.respond(
                "⚠️ Session اکانت متصل پاک شود و QR Login جدید شروع شود؟",
                buttons=[[Button.inline("✅ بله", b"reauth_confirm"), Button.inline("❌ لغو", b"reauth_cancel")]],
            )
            return
        if text in {"/start", "/menu"}:
            self.pop_state(event.sender_id)
            await self.send_home(event)
            return

        forwarded_channel_id = self.extract_forwarded_channel_id(event.message)
        if forwarded_channel_id is not None:
            self.pop_state(event.sender_id)
            await self.register_channel_from_ref(event, forwarded_channel_id)
            return

        state = self.states.get(event.sender_id)
        if state and self.numeric_state(state) and self.message_has_image(event.message):
            detected = await self.detect_number_from_image(event)
            if detected is None:
                await event.respond("🔎 عدد واضحی پیدا نکردم. عکس واضح‌تر یا عدد متنی بفرست.")
                return
            original_state = dict(state)
            self.set_state(event.sender_id, "ocr_confirm", detected=detected, original_state=original_state)
            await event.respond(
                f"🔎 عدد **{fmt_num(detected)}** تشخیص داده شد. استفاده شود؟",
                buttons=[[Button.inline(f"✅ بله، {fmt_num(detected)}", b"ocr_confirm")], [Button.inline("🔄 دوباره می‌فرستم", b"ocr_retry")]],
            )
            return

        if state and await self.handle_state_message(event, state, text):
            return
        if NUMBER_RE.fullmatch(text):
            await self.register_channel_from_ref(event, int(text))
            return
        if text.startswith("@"):
            await self.register_channel_from_ref(event, text)
            return
        await event.respond("دستور را نشناختم. `/start` را بزن.", buttons=[[Button.inline("🏠 منو", b"home")]])

    @staticmethod
    def extract_forwarded_channel_id(message: Any) -> int | None:
        header = getattr(message, "fwd_from", None)
        if header is None:
            return None
        for peer in [getattr(header, "from_id", None), getattr(header, "saved_from_peer", None)]:
            if isinstance(peer, types.PeerChannel):
                return marked_peer_id(peer)
        return None

    async def handle_state_message(self, event: events.NewMessage.Event, state: dict[str, Any], text: str) -> bool:
        name = state.get("name")
        if name == "ocr_confirm":
            await event.respond("برای استفاده از عدد تشخیص‌داده‌شده دکمه تأیید را بزن یا /cancel.")
            return True
        if name == "await_channel":
            if not (NUMBER_RE.fullmatch(text) or text.startswith("@")):
                await event.respond("آیدی `-100...`، یوزرنیم یا پیام فورواردشده بفرست.")
                return True
            self.pop_state(event.sender_id)
            await self.register_channel_from_ref(event, int(text) if NUMBER_RE.fullmatch(text) else text)
            return True
        if name == "global_approve_custom":
            if not text.isdigit() or int(text) <= 0:
                await event.respond("یک عدد بیشتر از صفر بفرست؛ مثال `20`.")
                return True
            self.pop_state(event.sender_id)
            await self.confirm_global_random(event, int(text))
            return True
        if name == "link_approve_custom":
            if not text.isdigit() or int(text) <= 0:
                await event.respond("یک عدد بیشتر از صفر بفرست.")
                return True
            self.pop_state(event.sender_id)
            await self.run_link_random(event, state["invite_key"], int(text))
            return True
        if name == "create_title":
            title = text.strip()
            if title in {"-", "بدون نام"}:
                title = f"Link {datetime.now().strftime('%m-%d %H:%M')}"
            if not title:
                await event.respond("عنوان بفرست یا `-` برای عنوان خودکار.")
                return True
            self.set_state(event.sender_id, "create_wait_mode", title=title[:32])
            await event.respond(
                "نوع عضویت این لینک را انتخاب کن:",
                buttons=[[Button.inline("🛂 نیازمند تأیید مدیر", b"create_mode:approval")], [Button.inline("🚪 ورود مستقیم", b"create_mode:direct")]],
            )
            return True
        if name == "create_expire_custom":
            if not text.isdigit() or int(text) <= 0:
                await event.respond("تعداد ساعت را به‌صورت عدد بفرست؛ مثال `36`.")
                return True
            payload = dict(state)
            hours = int(text)
            await self.choose_create_expiry(event, payload, hours * 3600)
            return True
        if name == "create_auto_custom":
            if not text.isdigit():
                await event.respond("عدد بفرست؛ `0` یعنی Auto Approve خاموش.")
                return True
            payload = dict(state)
            await self.choose_create_auto(event, payload, int(text))
            return True
        if name == "create_usage_custom":
            if not text.isdigit():
                await event.respond("عدد بفرست؛ `0` یعنی بدون محدودیت عضو.")
                return True
            payload = dict(state)
            self.pop_state(event.sender_id)
            await self.finish_create_link(event, payload, usage_limit=int(text) or None)
            return True
        if name == "set_auto_limit":
            if not text.isdigit():
                await event.respond("عدد بفرست؛ `0` یعنی خاموش.")
                return True
            self.pop_state(event.sender_id)
            await self.set_auto_limit(event, state["invite_key"], int(text))
            return True
        if name == "edit_title":
            title = text.strip()
            if not title:
                await event.respond("عنوان خالی نباشد.")
                return True
            self.pop_state(event.sender_id)
            await self.edit_link_title(event, state["invite_key"], title[:32])
            return True
        if name == "edit_expire_custom":
            if not text.isdigit() or int(text) <= 0:
                await event.respond("تعداد ساعت را بفرست؛ مثال `48`.")
                return True
            self.pop_state(event.sender_id)
            await self.edit_link_expiry(event, state["invite_key"], int(text) * 3600)
            return True
        return False

    async def on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self.is_owner(event.sender_id):
            await event.answer("دسترسی نداری", alert=True)
            return
        data = event.data.decode("utf-8", errors="ignore")
        await event.answer()

        if data == "noop":
            return
        if data == "reauth":
            await event.respond(
                "⚠️ Session فعلی پاک شود و اکانت دیگری با QR متصل شود؟",
                buttons=[[Button.inline("✅ بله، تغییر اکانت", b"reauth_confirm"), Button.inline("❌ لغو", b"reauth_cancel")]],
            )
        elif data == "reauth_cancel":
            await event.respond("تغییر اکانت لغو شد.", buttons=[[Button.inline("🏠 منو", b"home")]])
        elif data == "reauth_confirm":
            await event.respond("⏳ در حال پاک‌کردن Session و بازکردن QR Setup…")
            await self.vault.clear()
            self.reauth_requested = True
            await self.bot.disconnect()
        elif data == "ocr_confirm":
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "ocr_confirm":
                await event.respond("این تشخیص منقضی شده؛ دوباره بفرست.")
                return
            original = dict(state.get("original_state") or {})
            detected = int(state.get("detected", 0))
            if not original or detected < 0:
                self.pop_state(event.sender_id)
                await event.respond("ورودی OCR معتبر نیست.")
                return
            self.states[event.sender_id] = original
            await self.handle_state_message(event, original, str(detected))
        elif data == "ocr_retry":
            state = self.states.get(event.sender_id)
            if state and state.get("name") == "ocr_confirm":
                self.states[event.sender_id] = dict(state.get("original_state") or {})
            await event.respond("عکس جدید یا عدد متنی را بفرست.")
        elif data in {"home", "dashboard"}:
            await self.render_dashboard(event, edit=True)
        elif data == "scan_channels":
            await self.scan_channels(event)
        elif data == "ask_channel":
            self.set_state(event.sender_id, "await_channel")
            await event.respond("آیدی کانال مثل `-1001234567890` را بفرست یا یک پیام از کانال فوروارد کن.")
        elif data == "setup_help":
            await event.edit(self.setup_help_text(), buttons=[[Button.inline("🔙 برگشت", b"home")]])
        elif data == "help":
            await event.edit(self.help_text(), buttons=[[Button.inline("🔙 منو", b"home")]])
        elif data == "channels":
            await self.render_channels(event)
        elif data.startswith("select_channel:"):
            item = self.store.get_channel_by_key(data.split(":", 1)[1])
            if item:
                await self.store.set_active_channel(int(item["id"]))
                await self.render_dashboard(event, edit=True)
        elif data == "channel_requests":
            await self.render_channel_requests(event)
        elif data.startswith("global_confirm:"):
            await self.confirm_global_random(event, int(data.split(":", 1)[1]))
        elif data.startswith("global_run:"):
            await self.run_global_random(event, int(data.split(":", 1)[1]))
        elif data == "global_custom":
            self.set_state(event.sender_id, "global_approve_custom")
            await event.respond("چند نفر از **کل صف کانال** به‌صورت تصادفی تأیید شوند؟ عدد یا عکس واضح عدد را بفرست.")
        elif data == "global_all_confirm":
            await self.confirm_global_all(event)
        elif data == "global_all_run":
            await self.run_global_all(event)
        elif data == "links":
            await self.render_links(event, page=0)
        elif data.startswith("links:"):
            await self.render_links(event, page=int(data.split(":", 1)[1]))
        elif data.startswith("invite:"):
            await self.render_invite(event, data.split(":", 1)[1])
        elif data.startswith("linkapprove:"):
            _, key, amount = data.split(":", 2)
            if amount == "custom":
                self.set_state(event.sender_id, "link_approve_custom", invite_key=key)
                await event.respond("چند نفر از همین لینک تصادفی تأیید شوند؟ عدد یا عکس عدد را بفرست.")
            elif amount == "all":
                await self.run_link_all(event, key)
            else:
                await self.run_link_random(event, key, int(amount))
        elif data.startswith("linkallrun:"):
            await self.execute_link_all(event, data.split(":", 1)[1])
        elif data == "create_link":
            if not self.active_channel():
                await event.edit("اول کانال انتخاب کن.", buttons=self.onboarding_buttons())
                return
            self.set_state(event.sender_id, "create_title")
            await event.respond("عنوان مدیریتی لینک را بفرست؛ برای عنوان خودکار `-` بفرست.")
        elif data.startswith("create_mode:"):
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "create_wait_mode":
                await event.respond("فرایند ساخت منقضی شده؛ دوباره «ساخت لینک» را بزن.")
                return
            state["mode"] = data.split(":", 1)[1]
            self.states[event.sender_id] = {"name": "create_wait_expiry", **{k: v for k, v in state.items() if k != "name"}}
            await self.send_create_expiry_menu(event)
        elif data.startswith("create_exp:"):
            value = data.split(":", 1)[1]
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "create_wait_expiry":
                await event.respond("فرایند ساخت منقضی شده.")
                return
            if value == "custom":
                self.states[event.sender_id] = {"name": "create_expire_custom", **{k: v for k, v in state.items() if k != "name"}}
                await event.respond("لینک چند ساعت اعتبار داشته باشد؟ مثال `36`.")
            else:
                await self.choose_create_expiry(event, dict(state), int(value))
        elif data.startswith("create_auto:"):
            value = data.split(":", 1)[1]
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "create_wait_auto":
                await event.respond("فرایند ساخت منقضی شده.")
                return
            if value == "custom":
                self.states[event.sender_id] = {"name": "create_auto_custom", **{k: v for k, v in state.items() if k != "name"}}
                await event.respond("سقف Auto Approve را بفرست؛ `0` یعنی خاموش.")
            else:
                await self.choose_create_auto(event, dict(state), int(value))
        elif data.startswith("create_report:"):
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "create_wait_report":
                await event.respond("فرایند ساخت منقضی شده.")
                return
            enabled = data.endswith(":on")
            state["report_enabled"] = enabled
            self.pop_state(event.sender_id)
            await self.finish_create_link(event, state)
        elif data.startswith("create_usage:"):
            value = data.split(":", 1)[1]
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "create_wait_usage":
                await event.respond("فرایند ساخت منقضی شده.")
                return
            if value == "custom":
                self.states[event.sender_id] = {"name": "create_usage_custom", **{k: v for k, v in state.items() if k != "name"}}
                await event.respond("حداکثر چند عضو از لینک مستقیم وارد شوند؟ `0` یعنی بدون محدودیت.")
            else:
                self.pop_state(event.sender_id)
                await self.finish_create_link(event, state, usage_limit=int(value) or None)
        elif data.startswith("auto_limit:"):
            key = data.split(":", 1)[1]
            self.set_state(event.sender_id, "set_auto_limit", invite_key=key)
            await event.respond("سقف کل Auto Approve را بفرست؛ `0` یعنی خاموش. عکس عدد هم قبول است.")
        elif data.startswith("disable_auto:"):
            key = data.split(":", 1)[1]
            invite = await self.get_invite(key)
            channel = self.active_channel()
            if invite and channel:
                await self.store.disable_policy(int(channel["id"]), invite.link)
            await self.render_invite(event, key)
        elif data.startswith("toggle_report:"):
            await self.toggle_link_report(event, data.split(":", 1)[1])
        elif data.startswith("edit_menu:"):
            await self.render_edit_menu(event, data.split(":", 1)[1])
        elif data.startswith("edit_title:"):
            key = data.split(":", 1)[1]
            self.set_state(event.sender_id, "edit_title", invite_key=key)
            await event.respond("عنوان جدید لینک را بفرست.")
        elif data.startswith("edit_exp_menu:"):
            await self.send_edit_expiry_menu(event, data.split(":", 1)[1])
        elif data.startswith("edit_exp:"):
            _, key, value = data.split(":", 2)
            if value == "custom":
                self.set_state(event.sender_id, "edit_expire_custom", invite_key=key)
                await event.respond("از الان چند ساعت اعتبار داشته باشد؟ مثال `48`.")
            else:
                await self.edit_link_expiry(event, key, int(value))
        elif data.startswith("revoke_confirm:"):
            key = data.split(":", 1)[1]
            await event.edit(
                "⚠️ این لینک باطل شود؟ بعد از Revoke دیگر قابل استفاده نیست.",
                buttons=[[Button.inline("🚫 بله، باطل کن", f"revoke_run:{key}".encode())], [Button.inline("🔙 لغو", f"invite:{key}".encode())]],
            )
        elif data.startswith("revoke_run:"):
            await self.revoke_link(event, data.split(":", 1)[1])

    async def send_home(self, event: Any) -> None:
        if not self.store.channels():
            await event.respond(self.onboarding_text(), buttons=self.onboarding_buttons())
            return
        if not self.active_channel():
            await event.respond("یک کانال انتخاب کن.", buttons=self.channel_buttons())
            return
        await self.render_dashboard(event, edit=False)

    async def register_channel_from_ref(self, event: Any, ref: int | str) -> None:
        status = await event.respond("⏳ در حال شناسایی کانال و دسترسی‌ها…")
        try:
            channel = await self.service.register_channel(ref)
            level = "👑 Owner" if channel.is_creator else "🛡 Admin"
            await status.edit(
                f"✅ کانال فعال شد.\n\nنام: **{channel.title}**\nآیدی: `{channel.id}`\nسطح Session: **{level}**",
                buttons=[[Button.inline("🏠 بازکردن داشبورد", b"dashboard")]],
            )
        except Exception as exc:
            await status.edit(f"❌ کانال فعال نشد.\n`{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙 راه اتصال", b"setup_help")]])

    async def scan_channels(self, event: Any) -> None:
        await event.edit("⏳ در حال اسکن کانال‌های قابل مدیریت…")
        try:
            channels = await self.service.scan_manageable_channels()
        except Exception as exc:
            await event.edit(f"❌ اسکن نشد: `{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙", b"home")]])
            return
        if not channels:
            await event.edit("کانال قابل مدیریت پیدا نشد.", buttons=self.onboarding_buttons())
            return
        rows: list[list[Button]] = []
        for channel in channels:
            await self.store.upsert_channel(channel.as_store_item(), make_active=False)
            key = short_hash(str(channel.id), 10)
            badge = "👑" if channel.is_creator else "🛡"
            rows.append([Button.inline(f"{badge} {compact_title(channel.title, str(channel.id))}", f"select_channel:{key}".encode())])
        rows.append([Button.inline("🔙 منو", b"home")])
        await event.edit("کانال را انتخاب کن:", buttons=rows)

    def channel_buttons(self) -> list[list[Button]]:
        rows: list[list[Button]] = []
        active_id = self.store.active_channel_id
        for item in self.store.channels():
            key = short_hash(str(item["id"]), 10)
            prefix = "✅" if int(item["id"]) == active_id else "📺"
            owner_badge = "👑" if item.get("is_creator") else "🛡"
            rows.append([Button.inline(f"{prefix}{owner_badge} {compact_title(item.get('title'), str(item['id']))}", f"select_channel:{key}".encode())])
        rows.extend([[Button.inline("🔎 اسکن", b"scan_channels")], [Button.inline("➕ افزودن با آیدی یا فوروارد", b"ask_channel")], [Button.inline("🔙 منو", b"home")]])
        return rows

    async def render_channels(self, event: Any) -> None:
        active = self.active_channel()
        text = "📺 **کانال‌های ثبت‌شده**"
        if active:
            text += f"\n\nفعال: **{active.get('title')}**\n`{active.get('id')}`"
        await event.edit(text, buttons=self.channel_buttons())

    async def render_dashboard(self, event: Any, edit: bool) -> None:
        channel = self.active_channel()
        if not channel:
            target = event.edit if edit else event.respond
            await target(self.onboarding_text(), buttons=self.onboarding_buttons())
            return
        target = event if edit else await event.respond("⏳ در حال دریافت وضعیت کانال…")
        if edit:
            await event.edit("⏳ در حال دریافت وضعیت کانال…")
        try:
            overview = await self.service.channel_overview(int(channel["id"]))
            invites = await self.service.list_invites(int(channel["id"]))
        except Exception as exc:
            await target.edit(f"❌ دریافت داشبورد شکست خورد.\n`{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔄 دوباره", b"dashboard"), Button.inline("📺 کانال‌ها", b"channels")]])
            return
        active_invites = [i for i in invites if i.active]
        approval_invites = [i for i in active_invites if i.request_needed]
        level = "👑 Owner — دسترسی کامل" if overview.is_creator else "🛡 Admin — دسترسی لینک‌ها محدود به لینک‌های خودش"
        lines = [
            f"🏠 **{channel.get('title', 'کانال')}**",
            "",
            f"👥 اعضا: **{fmt_num(overview.members)} نفر**",
            f"⏳ کل درخواست‌های در انتظار کانال: **{fmt_num(overview.pending)} نفر**",
            f"🔗 لینک‌های قابل مشاهده برای Session: **{fmt_num(len(active_invites))}**",
            f"🛂 لینک‌های تأییدی قابل مشاهده: **{fmt_num(len(approval_invites))}**",
            f"🔐 سطح دسترسی: **{level}**",
        ]
        if not overview.is_creator and overview.pending > 0:
            lines.extend(["", "⚠️ عدد کل Pending از خود کانال خوانده شده، اما برای گرفتن فهرست کامل درخواست‌های لینک‌های سایر ادمین‌ها باید Owner را با QR متصل کنی."])
        await target.edit("\n".join(lines), buttons=self.dashboard_buttons())

    async def render_channel_requests(self, event: Any) -> None:
        channel = self.active_channel()
        if not channel:
            await event.edit("کانال فعال نیست.", buttons=[[Button.inline("🔙", b"home")]])
            return
        await event.edit("⏳ در حال خواندن صف کلی کانال…")
        try:
            overview = await self.service.channel_overview(int(channel["id"]))
        except Exception as exc:
            await event.edit(f"❌ خطا: `{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙", b"dashboard")]])
            return
        lines = [
            "⏳ **مدیریت درخواست‌های کانال**",
            f"کانال: **{channel.get('title')}**",
            f"کل Pending فعلی: **{fmt_num(overview.pending)} نفر**",
            "",
        ]
        rows: list[list[Button]] = []
        if overview.is_creator:
            lines.append("🎲 Random این بخش از **کل صف قابل دسترسی کانال** نمونه‌گیری می‌کند، نه فقط اولین صفحه.")
            rows.extend([
                [Button.inline("🎲 10 نفر", b"global_confirm:10"), Button.inline("🎲 50 نفر", b"global_confirm:50")],
                [Button.inline("🎲 100 نفر", b"global_confirm:100"), Button.inline("🔢 تعداد دلخواه", b"global_custom")],
                [Button.inline("⚡ تأیید همه Pendingها", b"global_all_confirm")],
            ])
        else:
            lines.extend([
                "⚠️ Session فعلی Owner نیست.",
                "Telegram اجازه گرفتن فهرست کامل درخواست‌های لینک‌های ساخته‌شده توسط سایر ادمین‌ها را به Admin معمولی نمی‌دهد.",
                "برای Random واقعی از کل صف، Owner کانال را متصل کن. مدیریت درخواست‌های لینک‌های خود این اکانت از بخش «مدیریت لینک‌ها» همچنان فعال است.",
            ])
            rows.append([Button.inline("🔗 درخواست‌های لینک‌های قابل مدیریت", b"links")])
        rows.append([Button.inline("🔄 بروزرسانی", b"channel_requests"), Button.inline("🔙 داشبورد", b"dashboard")])
        await event.edit("\n".join(lines), buttons=rows)

    async def confirm_global_random(self, event: Any, amount: int) -> None:
        channel = self.active_channel()
        if not channel:
            await event.respond("کانال فعال نیست.")
            return
        try:
            overview = await self.service.channel_overview(int(channel["id"]))
        except Exception as exc:
            await event.respond(f"❌ خطا: `{type(exc).__name__}: {exc}`")
            return
        if not overview.is_creator:
            await event.respond("❌ برای Random از کل صف، Session باید Owner کانال باشد.")
            return
        if overview.pending <= 0:
            await event.respond("درخواستی در صف نیست.")
            return
        amount = min(amount, overview.pending)
        if amount <= 0:
            await event.respond("تعداد انتخاب‌شده معتبر نیست.")
            return
        await event.respond(
            f"⚠️ از بین **{fmt_num(overview.pending)}** درخواست فعلی، **{fmt_num(amount)} نفر** به‌صورت تصادفی انتخاب و تأیید شوند؟",
            buttons=[[Button.inline("✅ انجام بده", f"global_run:{amount}".encode()), Button.inline("❌ لغو", b"channel_requests")]],
        )

    async def run_global_random(self, event: Any, amount: int) -> None:
        channel = self.active_channel()
        if not channel:
            return
        msg = await event.respond(f"⏳ در حال نمونه‌گیری تصادفی از کل صف برای **{fmt_num(amount)} نفر**…")
        last_edit = 0.0

        async def scan_progress(seen: int, total: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < 2:
                return
            last_edit = now
            try:
                await msg.edit(f"🔎 در حال اسکن صف… **{fmt_num(seen)} / {fmt_num(total)}** درخواست بررسی شد.")
            except RPCError:
                pass

        async def progress(done: int, total: int, success: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if done != total and now - last_edit < 2:
                return
            last_edit = now
            try:
                await msg.edit(f"✅ در حال تأیید… **{fmt_num(done)}/{fmt_num(total)}**\nموفق: **{fmt_num(success)}**")
            except RPCError:
                pass

        try:
            success, selected, scanned, errors = await self.service.approve_random(
                int(channel["id"]), amount, link=None, progress=progress, scan_progress=scan_progress
            )
            overview = await self.service.channel_overview(int(channel["id"]))
            err = f"\nخطاها: `{', '.join(sorted(set(errors))[:5])}`" if errors else ""
            await msg.edit(
                f"✅ Random Approve تمام شد.\n\nصف اسکن‌شده: **{fmt_num(scanned)}**\nانتخاب‌شده: **{fmt_num(selected)}**\nتأیید موفق: **{fmt_num(success)}**\nPending باقی‌مانده: **{fmt_num(overview.pending)}**{err}",
                buttons=[[Button.inline("⏳ مدیریت درخواست‌ها", b"channel_requests"), Button.inline("🏠 داشبورد", b"dashboard")]],
            )
        except OwnerRequiredError as exc:
            await msg.edit(f"❌ {exc}", buttons=[[Button.inline("🔙", b"channel_requests")]])
        except Exception as exc:
            await msg.edit(f"❌ عملیات شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def confirm_global_all(self, event: Any) -> None:
        channel = self.active_channel()
        if not channel:
            return
        overview = await self.service.channel_overview(int(channel["id"]))
        if not overview.is_creator:
            await event.respond("❌ برای تأیید کل صف، Session باید Owner باشد.")
            return
        if overview.pending <= 0:
            await event.respond("درخواستی در صف نیست.")
            return
        await event.edit(
            f"⚠️ **همه {fmt_num(overview.pending)} درخواست فعلی کانال** تأیید شوند؟",
            buttons=[[Button.inline("⚡ بله، همه", b"global_all_run")], [Button.inline("❌ لغو", b"channel_requests")]],
        )

    async def run_global_all(self, event: Any) -> None:
        channel = self.active_channel()
        if not channel:
            return
        await event.edit("⏳ در حال تأیید تمام درخواست‌های کانال…")
        try:
            await self.service.approve_all(int(channel["id"]), link=None)
            overview = await self.service.channel_overview(int(channel["id"]))
            await event.edit(f"✅ درخواست‌ها پردازش شدند.\nPending فعلی: **{fmt_num(overview.pending)}**", buttons=[[Button.inline("🔙 درخواست‌ها", b"channel_requests")]])
        except Exception as exc:
            await event.edit(f"❌ عملیات شکست خورد.\n`{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙", b"channel_requests")]])

    async def render_links(self, event: Any, page: int = 0) -> None:
        channel = self.active_channel()
        if not channel:
            await event.edit("کانال فعال نیست.", buttons=[[Button.inline("🔙", b"home")]])
            return
        await event.edit("⏳ در حال دریافت لینک‌های قابل مدیریت…")
        try:
            overview = await self.service.channel_overview(int(channel["id"]))
            invites = await self.service.list_invites(int(channel["id"]))
            await self.service.enrich_pending_counts(int(channel["id"]), invites)
        except Exception as exc:
            await event.edit(f"❌ خطا: `{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙", b"dashboard")]])
            return
        for invite in invites:
            self.invite_cache[(int(channel["id"]), invite.key)] = invite
        active = [i for i in invites if i.active]
        total_pages = max(1, (len(active) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        shown = active[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]
        lines = ["🔗 **مدیریت لینک‌ها**", f"لینک‌های قابل مشاهده: **{fmt_num(len(active))}**"]
        if not overview.is_creator:
            lines.append("⚠️ چون Session Owner نیست، Telegram فقط لینک‌های ساخته‌شده توسط همین اکانت را تضمین می‌کند.")
        rows: list[list[Button]] = []
        for invite in shown:
            if invite.request_needed:
                label = f"🛂 {compact_title(invite.title, 'لینک')} • {fmt_num(invite.pending)} Pending"
            else:
                label = f"🔗 {compact_title(invite.title, 'لینک')} • ورود مستقیم"
            rows.append([Button.inline(label, f"invite:{invite.key}".encode())])
        if not shown:
            lines.append("\nلینک قابل مدیریتی پیدا نشد.")
        if total_pages > 1:
            nav: list[Button] = []
            if page > 0:
                nav.append(Button.inline("◀️ قبلی", f"links:{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{total_pages}", b"noop"))
            if page + 1 < total_pages:
                nav.append(Button.inline("بعدی ▶️", f"links:{page + 1}".encode()))
            rows.append(nav)
        rows.extend([[Button.inline("➕ ساخت لینک جدید", b"create_link")], [Button.inline("🔄 بروزرسانی", f"links:{page}".encode()), Button.inline("🔙 داشبورد", b"dashboard")]])
        await event.edit("\n".join(lines), buttons=rows)

    async def get_invite(self, invite_key: str) -> InviteInfo | None:
        channel = self.active_channel()
        if not channel:
            return None
        cache_key = (int(channel["id"]), invite_key)
        cached = self.invite_cache.get(cache_key)
        if cached:
            try:
                if cached.request_needed and cached.active:
                    cached.pending = await self.service.pending_count(int(channel["id"]), cached.link)
                return cached
            except RPCError:
                pass
        invite = await self.service.find_invite(int(channel["id"]), invite_key)
        if invite:
            self.invite_cache[cache_key] = invite
        return invite

    def policy_consumed(self, policy: dict[str, Any], invite: InviteInfo) -> int:
        by_bot = int(policy.get("approved_by_bot", 0))
        usage_since_creation = max(0, int(invite.usage) - int(policy.get("initial_usage", 0)))
        return max(by_bot, usage_since_creation)

    async def render_invite(self, event: Any, invite_key: str) -> None:
        await event.edit("⏳ در حال دریافت جزئیات لینک…")
        channel = self.active_channel()
        if not channel:
            return
        try:
            invite = await self.get_invite(invite_key)
        except Exception as exc:
            await event.edit(f"❌ خطا: `{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙 لینک‌ها", b"links")]])
            return
        if not invite:
            await event.edit("لینک پیدا نشد یا دیگر برای این Session قابل مشاهده نیست.", buttons=[[Button.inline("🔙 لینک‌ها", b"links")]])
            return
        policy = self.store.get_policy(int(channel["id"]), invite.link)
        lines = [
            f"🔗 **{invite.title}**",
            f"`{invite.link}`",
            "",
            f"نوع: **{'تأیید مدیر' if invite.request_needed else 'ورود مستقیم'}**",
            f"Pending: **{fmt_num(invite.pending)} نفر**",
            f"عضوشده: **{fmt_num(invite.usage)} نفر**",
            f"انقضا: **{format_expiry(invite.expire_date)}**",
            f"سازنده: **{invite.admin_name}**",
        ]
        if invite.usage_limit:
            lines.append(f"محدودیت عضو: **{fmt_num(invite.usage_limit)}**")
        if policy:
            maximum = int(policy.get("max_approvals", 0))
            consumed = self.policy_consumed(policy, invite)
            remaining = max(0, maximum - consumed)
            auto_status = "فعال" if policy.get("enabled") and remaining > 0 else "خاموش/تکمیل"
            report_status = "فعال" if policy.get("report_enabled") else "خاموش"
            lines.extend(["", f"🤖 Auto Approve: **{auto_status}** • سقف {fmt_num(maximum)} • باقی‌مانده {fmt_num(remaining)}", f"📨 گزارش یک‌دقیقه‌ای: **{report_status}**"])
        else:
            lines.extend(["", "🤖 Auto Approve: **خاموش**", "📨 گزارش یک‌دقیقه‌ای: **خاموش**"])

        rows: list[list[Button]] = []
        if invite.request_needed and invite.active:
            rows.extend([
                [Button.inline("🎲 10", f"linkapprove:{invite.key}:10".encode()), Button.inline("🎲 50", f"linkapprove:{invite.key}:50".encode()), Button.inline("🎲 100", f"linkapprove:{invite.key}:100".encode())],
                [Button.inline("🔢 تعداد دلخواه", f"linkapprove:{invite.key}:custom".encode()), Button.inline("⚡ همه همین لینک", f"linkapprove:{invite.key}:all".encode())],
                [Button.inline("🤖 تنظیم Auto Approve", f"auto_limit:{invite.key}".encode())],
            ])
            if policy and policy.get("enabled"):
                rows.append([Button.inline("⏸ توقف Auto Approve", f"disable_auto:{invite.key}".encode())])
        report_on = bool(policy and policy.get("report_enabled"))
        rows.append([Button.inline("🔕 خاموش‌کردن گزارش دقیقه‌ای" if report_on else "🔔 فعال‌کردن گزارش دقیقه‌ای", f"toggle_report:{invite.key}".encode())])
        rows.append([Button.inline("✏️ تنظیمات لینک", f"edit_menu:{invite.key}".encode())])
        rows.append([Button.inline("🔄 بروزرسانی", f"invite:{invite.key}".encode()), Button.inline("🔙 لینک‌ها", b"links")])
        await event.edit("\n".join(lines), buttons=rows)

    async def run_link_random(self, event: Any, invite_key: str, amount: int) -> None:
        channel = self.active_channel()
        if not channel:
            return
        invite = await self.get_invite(invite_key)
        if not invite or not invite.request_needed:
            await event.respond("این لینک قابل تأیید نیست.")
            return
        msg = await event.respond(f"⏳ در حال انتخاب تصادفی **{fmt_num(amount)} نفر** از همین لینک…")
        last_edit = 0.0

        async def scan_progress(seen: int, total: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit >= 2:
                last_edit = now
                try:
                    await msg.edit(f"🔎 اسکن درخواست‌های همین لینک: **{fmt_num(seen)}/{fmt_num(total)}**")
                except RPCError:
                    pass

        async def progress(done: int, total: int, success: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if done == total or now - last_edit >= 2:
                last_edit = now
                try:
                    await msg.edit(f"⏳ تأیید: **{fmt_num(done)}/{fmt_num(total)}** • موفق {fmt_num(success)}")
                except RPCError:
                    pass

        try:
            success, selected, scanned, errors = await self.service.approve_random(int(channel["id"]), amount, invite.link, progress, scan_progress)
            policy = self.store.get_policy(int(channel["id"]), invite.link)
            if success and policy:
                await self.store.update_policy_progress(int(channel["id"]), invite.link, success_delta=success)
            remaining = await self.service.pending_count(int(channel["id"]), invite.link)
            err = f"\nخطاها: `{', '.join(sorted(set(errors))[:5])}`" if errors else ""
            await msg.edit(f"✅ تمام شد.\nاسکن: **{fmt_num(scanned)}**\nانتخاب: **{fmt_num(selected)}**\nموفق: **{fmt_num(success)}**\nباقی‌مانده لینک: **{fmt_num(remaining)}**{err}", buttons=[[Button.inline("🔙 همین لینک", f"invite:{invite.key}".encode())]])
        except Exception as exc:
            await msg.edit(f"❌ عملیات شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def run_link_all(self, event: Any, invite_key: str) -> None:
        channel = self.active_channel()
        if not channel:
            return
        invite = await self.get_invite(invite_key)
        if not invite or not invite.request_needed:
            return
        await event.edit(f"⚠️ همه درخواست‌های **{invite.title}** تأیید شوند؟", buttons=[[Button.inline("⚡ انجام بده", f"linkallrun:{invite.key}".encode())], [Button.inline("❌ لغو", f"invite:{invite.key}".encode())]])
        # Register one-shot callback dynamically through state-free data handled below via generic handler is avoided;
        # instead replace callback handler with direct data by adding a small temporary state marker.

    async def execute_link_all(self, event: Any, invite_key: str) -> None:
        channel = self.active_channel()
        invite = await self.get_invite(invite_key)
        if not channel or not invite:
            return
        await event.edit("⏳ در حال تأیید همه درخواست‌های همین لینک…")
        try:
            await self.service.approve_all(int(channel["id"]), invite.link)
            await event.edit("✅ همه درخواست‌های قابل دسترسی این لینک پردازش شدند.", buttons=[[Button.inline("🔙 لینک", f"invite:{invite.key}".encode())]])
        except Exception as exc:
            await event.edit(f"❌ خطا: `{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙 لینک", f"invite:{invite.key}".encode())]])

    async def send_create_expiry_menu(self, event: Any) -> None:
        await event.respond(
            "⏱ لینک چه مدت اعتبار داشته باشد؟",
            buttons=[
                [Button.inline("1 ساعت", b"create_exp:3600"), Button.inline("6 ساعت", b"create_exp:21600")],
                [Button.inline("1 روز", b"create_exp:86400"), Button.inline("3 روز", b"create_exp:259200")],
                [Button.inline("7 روز", b"create_exp:604800"), Button.inline("30 روز", b"create_exp:2592000")],
                [Button.inline("♾ بدون انقضا", b"create_exp:0"), Button.inline("✏️ ساعت دلخواه", b"create_exp:custom")],
            ],
        )

    async def choose_create_expiry(self, event: Any, state: dict[str, Any], seconds: int) -> None:
        payload = {k: v for k, v in state.items() if k != "name"}
        payload["expire_seconds"] = int(seconds)
        if payload.get("mode") == "approval":
            self.states[event.sender_id] = {"name": "create_wait_auto", **payload}
            await event.respond(
                "🤖 از درخواست‌های این لینک، حداکثر چند نفر خودکار تأیید شوند؟",
                buttons=[
                    [Button.inline("خاموش", b"create_auto:0"), Button.inline("10", b"create_auto:10")],
                    [Button.inline("50", b"create_auto:50"), Button.inline("100", b"create_auto:100")],
                    [Button.inline("🔢 دلخواه", b"create_auto:custom")],
                ],
            )
        else:
            self.states[event.sender_id] = {"name": "create_wait_usage", **payload}
            await event.respond(
                "👥 محدودیت تعداد عضو برای لینک مستقیم:",
                buttons=[
                    [Button.inline("بدون محدودیت", b"create_usage:0"), Button.inline("10", b"create_usage:10")],
                    [Button.inline("50", b"create_usage:50"), Button.inline("100", b"create_usage:100")],
                    [Button.inline("🔢 دلخواه", b"create_usage:custom")],
                ],
            )

    async def choose_create_auto(self, event: Any, state: dict[str, Any], maximum: int) -> None:
        payload = {k: v for k, v in state.items() if k != "name"}
        payload["auto_limit"] = int(maximum)
        self.states[event.sender_id] = {"name": "create_wait_report", **payload}
        await event.respond(
            "📨 اگر روی این لینک درخواست جدید آمد، هر یک دقیقه فقط در صورت وجود درخواست جدید گزارش بفرستم؟",
            buttons=[[Button.inline("✅ فعال", b"create_report:on"), Button.inline("❌ خاموش", b"create_report:off")]],
        )

    async def finish_create_link(self, event: Any, state: dict[str, Any], usage_limit: int | None = None) -> None:
        channel = self.active_channel()
        if not channel:
            return
        title = str(state.get("title") or "لینک")[:32]
        request_needed = state.get("mode") == "approval"
        seconds = int(state.get("expire_seconds", 0))
        expire_at = int(time.time()) + seconds if seconds > 0 else None
        auto_limit = int(state.get("auto_limit", 0)) if request_needed else 0
        report_enabled = bool(state.get("report_enabled", False)) if request_needed else False
        msg = await event.respond("⏳ در حال ساخت لینک…")
        try:
            invite = await self.service.create_invite(int(channel["id"]), title, request_needed, expire_at, usage_limit)
            self.invite_cache[(int(channel["id"]), invite.key)] = invite
            if request_needed and (auto_limit > 0 or report_enabled):
                await self.store.set_policy(int(channel["id"]), invite.link, invite.title, auto_limit, invite.usage, report_enabled=report_enabled)
            await msg.edit(
                "✅ لینک ساخته شد.\n\n"
                f"عنوان: **{invite.title}**\n"
                f"لینک: `{invite.link}`\n"
                f"نوع: **{'تأیید مدیر' if request_needed else 'ورود مستقیم'}**\n"
                f"انقضا: **{format_expiry(invite.expire_date)}**\n"
                + (f"Auto Approve: **{fmt_num(auto_limit) if auto_limit else 'خاموش'}**\n📨 گزارش دقیقه‌ای: **{'فعال' if report_enabled else 'خاموش'}**" if request_needed else f"محدودیت عضو: **{fmt_num(usage_limit) if usage_limit else 'ندارد'}**"),
                buttons=[[Button.inline("🔗 مدیریت همین لینک", f"invite:{invite.key}".encode()), Button.inline("🏠 داشبورد", b"dashboard")]],
            )
        except Exception as exc:
            await msg.edit(f"❌ ساخت لینک شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def set_auto_limit(self, event: Any, invite_key: str, maximum: int) -> None:
        channel = self.active_channel()
        if not channel:
            return
        msg = await event.respond("⏳ در حال ذخیره Auto Approve…")
        try:
            invite = await self.get_invite(invite_key)
            if not invite or not invite.request_needed:
                raise ValueError("این لینک تأییدی نیست.")
            existing = self.store.get_policy(int(channel["id"]), invite.link)
            initial_usage = int(existing.get("initial_usage", invite.usage)) if existing else invite.usage
            report_enabled = bool(existing and existing.get("report_enabled"))
            await self.store.set_policy(int(channel["id"]), invite.link, invite.title, maximum, initial_usage, report_enabled=report_enabled)
            await msg.edit(f"✅ Auto Approve روی **{fmt_num(maximum) if maximum else 'خاموش'}** تنظیم شد.", buttons=[[Button.inline("🔙 لینک", f"invite:{invite.key}".encode())]])
        except Exception as exc:
            await msg.edit(f"❌ تنظیم نشد.\n`{type(exc).__name__}: {exc}`")

    async def toggle_link_report(self, event: Any, invite_key: str) -> None:
        channel = self.active_channel()
        if not channel:
            return
        invite = await self.get_invite(invite_key)
        if not invite or not invite.request_needed:
            await event.respond("گزارش درخواست فقط برای لینک تأییدی معنی دارد.")
            return
        policy = self.store.get_policy(int(channel["id"]), invite.link)
        new_value = not bool(policy and policy.get("report_enabled"))
        await self.store.set_report_enabled(int(channel["id"]), invite.link, invite.title, invite.usage, new_value)
        await self.render_invite(event, invite_key)

    async def render_edit_menu(self, event: Any, invite_key: str) -> None:
        invite = await self.get_invite(invite_key)
        if not invite:
            await event.edit("لینک پیدا نشد.", buttons=[[Button.inline("🔙", b"links")]])
            return
        await event.edit(
            f"✏️ **تنظیمات {invite.title}**\n\nانقضا: **{format_expiry(invite.expire_date)}**",
            buttons=[
                [Button.inline("✏️ تغییر عنوان", f"edit_title:{invite.key}".encode())],
                [Button.inline("⏱ تغییر زمان انقضا", f"edit_exp_menu:{invite.key}".encode())],
                [Button.inline("🚫 باطل‌کردن لینک", f"revoke_confirm:{invite.key}".encode())],
                [Button.inline("🔙 بازگشت", f"invite:{invite.key}".encode())],
            ],
        )

    async def send_edit_expiry_menu(self, event: Any, invite_key: str) -> None:
        await event.edit(
            "⏱ از الان لینک تا چه مدت معتبر باشد؟",
            buttons=[
                [Button.inline("1 ساعت", f"edit_exp:{invite_key}:3600".encode()), Button.inline("6 ساعت", f"edit_exp:{invite_key}:21600".encode())],
                [Button.inline("1 روز", f"edit_exp:{invite_key}:86400".encode()), Button.inline("7 روز", f"edit_exp:{invite_key}:604800".encode())],
                [Button.inline("30 روز", f"edit_exp:{invite_key}:2592000".encode()), Button.inline("♾ بدون انقضا", f"edit_exp:{invite_key}:0".encode())],
                [Button.inline("✏️ ساعت دلخواه", f"edit_exp:{invite_key}:custom".encode())],
                [Button.inline("🔙", f"edit_menu:{invite_key}".encode())],
            ],
        )

    async def edit_link_title(self, event: Any, invite_key: str, title: str) -> None:
        channel = self.active_channel()
        invite = await self.get_invite(invite_key)
        if not channel or not invite:
            return
        msg = await event.respond("⏳ در حال تغییر عنوان…")
        try:
            updated = await self.service.edit_invite_title(int(channel["id"]), invite.link, title)
            self.invite_cache[(int(channel["id"]), updated.key)] = updated
            policy = self.store.get_policy(int(channel["id"]), invite.link)
            if policy:
                policy["title"] = updated.title
                await self.store.save("Rename invite policy")
            await msg.edit("✅ عنوان تغییر کرد.", buttons=[[Button.inline("🔙 لینک", f"invite:{updated.key}".encode())]])
        except Exception as exc:
            await msg.edit(f"❌ تغییر عنوان شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def edit_link_expiry(self, event: Any, invite_key: str, seconds: int) -> None:
        channel = self.active_channel()
        invite = await self.get_invite(invite_key)
        if not channel or not invite:
            return
        expire_at = int(time.time()) + seconds if seconds > 0 else None
        await event.edit("⏳ در حال تغییر انقضا…")
        try:
            updated = await self.service.edit_invite_expiry(int(channel["id"]), invite.link, expire_at)
            self.invite_cache[(int(channel["id"]), updated.key)] = updated
            await event.edit(f"✅ انقضا تغییر کرد: **{format_expiry(updated.expire_date)}**", buttons=[[Button.inline("🔙 لینک", f"invite:{updated.key}".encode())]])
        except Exception as exc:
            await event.edit(f"❌ تغییر انقضا شکست خورد.\n`{type(exc).__name__}: {exc}`", buttons=[[Button.inline("🔙", f"invite:{invite.key}".encode())]])

    async def revoke_link(self, event: Any, invite_key: str) -> None:
        channel = self.active_channel()
        invite = await self.get_invite(invite_key)
        if not channel or not invite:
            return
        await event.edit("⏳ در حال باطل‌کردن لینک…")
        try:
            await self.service.revoke_invite(int(channel["id"]), invite.link)
            policy = self.store.get_policy(int(channel["id"]), invite.link)
            if policy:
                policy["enabled"] = False
                policy["report_enabled"] = False
                await self.store.save("Disable revoked invite automation")
            self.invite_cache.pop((int(channel["id"]), invite.key), None)
            await event.edit("✅ لینک باطل شد.", buttons=[[Button.inline("🔙 لینک‌ها", b"links")]])
        except Exception as exc:
            await event.edit(f"❌ Revoke شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def auto_approval_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.run_auto_approval_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Automatic approval cycle failed")
            await asyncio.sleep(self.settings.auto_scan_seconds)

    async def run_auto_approval_cycle(self) -> None:
        policies = self.store.enabled_policies()
        if not policies:
            return
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for policy in policies:
            grouped[int(policy["channel_id"])].append(policy)
        for channel_id, channel_policies in grouped.items():
            try:
                invites = await self.service.list_invites(channel_id)
                by_link = {i.link: i for i in invites}
            except Exception as exc:
                log.warning("Could not list invites for auto approval %s: %s", channel_id, exc)
                continue
            for policy in channel_policies:
                invite = by_link.get(str(policy["link"]))
                if not invite or not invite.active or not invite.request_needed:
                    await self.store.update_policy_progress(channel_id, str(policy["link"]), enabled=False, last_error="invite_missing_or_inactive")
                    continue
                consumed = self.policy_consumed(policy, invite)
                maximum = int(policy.get("max_approvals", 0))
                quota_left = max(0, maximum - consumed)
                if quota_left <= 0:
                    await self.store.update_policy_progress(channel_id, invite.link, enabled=False, last_error=None)
                    await self.safe_notify_owner(f"🏁 سقف Auto Approve لینک **{invite.title}** کامل شد.\nسقف: **{fmt_num(maximum)}**")
                    continue
                try:
                    pending = await self.service.pending_count(channel_id, invite.link)
                    amount = min(pending, quota_left, self.settings.auto_approve_batch)
                    if amount <= 0:
                        continue
                    success, selected, _, errors = await self.service.approve_random(channel_id, amount, invite.link)
                    if success:
                        updated = await self.store.update_policy_progress(channel_id, invite.link, success_delta=success, last_error=None)
                        remaining = max(0, int(updated.get("max_approvals", 0)) - int(updated.get("approved_by_bot", 0))) if updated else 0
                        channel = self.store.get_channel(channel_id)
                        await self.safe_notify_owner(
                            f"🤖 **Auto Approve**\nکانال: **{(channel or {}).get('title', channel_id)}**\nلینک: **{invite.title}**\nانتخاب: **{fmt_num(selected)}**\nموفق: **{fmt_num(success)}**\nسهمیه باقی: **{fmt_num(remaining)}**"
                        )
                    elif errors:
                        await self.store.update_policy_progress(channel_id, invite.link, last_error=",".join(sorted(set(errors))[:5]))
                except FloodWaitError as exc:
                    log.warning("Auto approval FloodWait %s seconds", exc.seconds)
                except Exception as exc:
                    log.exception("Auto approval failed for %s", invite.link)
                    await self.store.update_policy_progress(channel_id, invite.link, last_error=type(exc).__name__)

    async def minute_report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.report_interval_seconds)
            try:
                await self.flush_minute_reports()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Minute report flush failed")

    async def flush_minute_reports(self) -> None:
        async with self.report_lock:
            if not self.report_events:
                return
            events_snapshot = dict(self.report_events)
            titles_snapshot = dict(self.report_titles)
            self.report_events.clear()
            self.report_titles.clear()
        grouped: dict[int, list[tuple[str, int, str]]] = defaultdict(list)
        for (channel_id, link), count in events_snapshot.items():
            grouped[channel_id].append((link, count, titles_snapshot.get((channel_id, link), "لینک")))
        for channel_id, items in grouped.items():
            channel = self.store.get_channel(channel_id) or {"title": str(channel_id)}
            lines = ["📥 **گزارش درخواست‌های جدید — یک دقیقه اخیر**", f"کانال: **{channel.get('title')}**", ""]
            total = 0
            for link, count, title in sorted(items, key=lambda x: -x[1]):
                total += count
                pending_text = ""
                try:
                    pending = await self.service.pending_count(channel_id, link)
                    pending_text = f" • Pending فعلی {fmt_num(pending)}"
                except Exception:
                    pass
                lines.append(f"🔗 **{title}**: +**{fmt_num(count)}** درخواست{pending_text}")
            lines.extend(["", f"مجموع درخواست جدید: **{fmt_num(total)} نفر**"])
            await self.safe_notify_owner("\n".join(lines), button_data=b"links", button_label="🔗 مدیریت لینک‌ها")

    async def safe_notify_owner(self, text: str, button_data: bytes = b"dashboard", button_label: str = "🏠 داشبورد") -> None:
        try:
            await self.bot.send_message(self.settings.owner_id, text, buttons=[[Button.inline(button_label, button_data)]])
        except RPCError:
            log.warning("Could not notify owner")

    def setup_help_text(self) -> str:
        return (
            "ℹ️ **اتصال کانال**\n\n"
            "• Bot را داخل کانال Admin کن و دسترسی Invite Users بده تا گزارش درخواست‌های جدید را دریافت کند.\n"
            "• اکانت QR شده نیز باید Admin و دارای Invite Users باشد.\n"
            "• برای مدیریت **کل صف و لینک‌های همه ادمین‌ها**، اکانت QR شده باید Owner کانال باشد.\n"
            "• کانال را با آیدی `-100...`، فوروارد یا اسکن اضافه کن."
        )

    def help_text(self) -> str:
        return (
            "ℹ️ **V6 — Channel Requests First**\n\n"
            "• داشبورد عدد واقعی Pending کل کانال را از ChannelFull می‌خواند.\n"
            "• اگر Session Owner باشد، Random Approve از کل صف با Reservoir Sampling انجام می‌شود؛ یعنی انتخاب فقط از صفحه اول نیست.\n"
            "• لینک‌ها منوی جدا دارند: ساخت لینک Approval/Direct، انقضا، محدودیت لینک مستقیم، تغییر عنوان/انقضا، Revoke.\n"
            "• Auto Approve را هنگام ساخت یا بعداً روی هر لینک تأییدی تنظیم کن.\n"
            "• گزارش یک‌دقیقه‌ای برای هر لینک قابل روشن/خاموش‌کردن است و فقط وقتی درخواست جدید واقعاً رسیده باشد پیام می‌دهد.\n"
            "• OCR برای تعدادها فعال است؛ Login همچنان فقط QR رسمی است.\n"
            "• اگر Session فقط Admin باشد، Telegram دسترسی کامل به لینک‌ها و درخواست‌های ساخته‌شده توسط ادمین‌های دیگر را نمی‌دهد؛ V6 این محدودیت را واضح نشان می‌دهد."
        )



async def run_manager(settings: Settings, vault: AuthVault) -> bool:
    store = Store(DATA_FILE, settings.persist_to_git)
    user_api = build_user_api(settings.owner_id)
    bot_api = build_bot_api(settings.owner_id)
    user_client = TelegramClient(StringSession(settings.user_session), api=user_api)
    bot_client = TelegramClient(StringSession(), api=bot_api)

    await user_client.connect()
    if not await user_client.is_user_authorized():
        await user_client.disconnect()
        raise RuntimeError("USER_SESSION_INVALID")

    await bot_client.start(bot_token=settings.bot_token)
    service = TelegramJoinService(user_client, bot_client, store, settings.approval_concurrency)
    control = ControlBot(bot_client, service, store, settings, vault)
    await control.start()

    me = await bot_client.get_me()
    log.info("Control bot @%s started for owner %s", me.username, settings.owner_id)
    try:
        account_line = ""
        if settings.connected_name or settings.connected_user_id:
            account_line = f"\n👤 اکانت متصل: **{settings.connected_name or 'Telegram User'}**" + (f" (`{settings.connected_user_id}`)" if settings.connected_user_id else "")
        await bot_client.send_message(
            settings.owner_id,
            "🟢 Telegram Join Manager V6 اجرا شد." + account_line,
            buttons=[[Button.inline("🏠 بازکردن داشبورد", b"home")]],
        )
    except RPCError:
        log.warning("Could not send startup message. Start the bot once in Telegram.")

    async def stop_later() -> None:
        await asyncio.sleep(settings.run_seconds)
        await bot_client.disconnect()

    stopper = asyncio.create_task(stop_later())
    try:
        await bot_client.run_until_disconnected()
    finally:
        stopper.cancel()
        if control.auto_task:
            control.auto_task.cancel()
        if control.report_task:
            control.report_task.cancel()
        await user_client.disconnect()
        if bot_client.is_connected():
            await bot_client.disconnect()
    return control.reauth_requested


async def main() -> None:
    required = ["BOT_TOKEN", "OWNER_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    bot_token = os.environ["BOT_TOKEN"].strip()
    owner_id = int(os.environ["OWNER_ID"])
    persist_to_git = os.getenv("PERSIST_TO_GIT", "true").lower() in {"1", "true", "yes", "on"}

    auth, vault = await ensure_auth_config(
        bot_token=bot_token,
        owner_id=owner_id,
        auth_file=AUTH_FILE,
        persist_to_git=persist_to_git,
    )
    while True:
        settings = Settings.load(auth)
        try:
            reauth = await run_manager(settings, vault)
        except RuntimeError as exc:
            if str(exc) != "USER_SESSION_INVALID":
                raise
            log.warning("Stored user session is invalid; starting QR setup again")
            await vault.clear()
            reauth = True
        if not reauth:
            break
        auth, vault = await ensure_auth_config(
            bot_token=bot_token,
            owner_id=owner_id,
            auth_file=AUTH_FILE,
            persist_to_git=persist_to_git,
            force_setup=True,
        )


if __name__ == "__main__":
    asyncio.run(main())

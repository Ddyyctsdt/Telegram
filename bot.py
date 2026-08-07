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
from dataclasses import dataclass
from datetime import datetime, timezone
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
log = logging.getLogger("telegram-join-manager")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unix_timestamp(value: Any | None) -> int | None:
    """Normalize Telethon date fields, which are datetime objects, to Unix seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def epoch_datetime() -> datetime:
    """Telegram date offsets are represented as datetime values by Telethon."""
    return datetime.fromtimestamp(0, tz=timezone.utc)


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def fmt_num(value: int | None) -> str:
    return f"{int(value or 0):,}"


def compact_title(value: str | None, fallback: str, limit: int = 34) -> str:
    text = (value or fallback).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_digits(value: str) -> str:
    return value.translate(PERSIAN_DIGITS)


def ocr_digits_from_bytes(blob: bytes) -> int | None:
    """OCR only for ordinary numeric admin inputs, never authentication secrets."""
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

    normalized = normalize_digits(raw)
    groups = re.findall(r"\d+", normalized)
    if not groups:
        return None
    # Prefer the longest group. A confirmation step prevents accidental execution.
    best = max(groups, key=len)
    try:
        return int(best)
    except ValueError:
        return None


def marked_peer_id(entity_or_peer: Any) -> int:
    return int(utils.get_peer_id(entity_or_peer))


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
            run_seconds=max(60, int(os.getenv("RUN_SECONDS", "20000"))),
            persist_to_git=os.getenv("PERSIST_TO_GIT", "true").lower()
            in {"1", "true", "yes", "on"},
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
            "version": 2,
            "active_channel_id": None,
            "channels": [],
            "policies": {},
        }

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            old_path = self.path.parent / "links.json"
            if old_path.exists():
                self._migrate_old_file(old_path)
            self.save_local()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("state root must be an object")
            raw.setdefault("version", 2)
            raw.setdefault("active_channel_id", None)
            raw.setdefault("channels", [])
            raw.setdefault("policies", {})
            self.data = raw
        except (OSError, ValueError, json.JSONDecodeError):
            log.exception("Could not read state file; starting with an empty state")
            self.data = self._empty_data()

    def _migrate_old_file(self, old_path: Path) -> None:
        try:
            raw = json.loads(old_path.read_text(encoding="utf-8"))
            channel = raw.get("channel") if isinstance(raw, dict) else None
            if channel and str(channel).lstrip("-").isdigit():
                channel_id = int(channel)
                self.data["channels"] = [
                    {
                        "id": channel_id,
                        "title": "کانال قبلی",
                        "username": None,
                        "added_at": utc_now_iso(),
                    }
                ]
                self.data["active_channel_id"] = channel_id
        except (OSError, ValueError, json.JSONDecodeError):
            log.exception("Old links.json migration failed")

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
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
            )
            if diff.returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True)
            subprocess.run(
                ["git", "pull", "--rebase", "--autostash"], cwd=ROOT, check=True
            )
            subprocess.run(["git", "push"], cwd=ROOT, check=True)
        except subprocess.CalledProcessError:
            log.exception("Could not persist state to git; local state is still updated")

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
        changed = len(self.data["channels"]) != before
        if not changed:
            return False
        if self.active_channel_id == int(channel_id):
            channels = self.channels()
            self.data["active_channel_id"] = int(channels[0]["id"]) if channels else None
        policies = self.data.setdefault("policies", {})
        for policy_key in list(policies):
            if int(policies[policy_key].get("channel_id", 0)) == int(channel_id):
                policies.pop(policy_key, None)
        await self.save("Remove Telegram channel")
        return True

    @staticmethod
    def policy_key(channel_id: int, link: str) -> str:
        return f"{int(channel_id)}:{short_hash(link, 16)}"

    def get_policy(self, channel_id: int, link: str) -> dict[str, Any] | None:
        return self.data.setdefault("policies", {}).get(self.policy_key(channel_id, link))

    def enabled_policies(self) -> list[dict[str, Any]]:
        return [
            item
            for item in self.data.setdefault("policies", {}).values()
            if bool(item.get("enabled"))
        ]

    async def set_policy(
        self,
        channel_id: int,
        link: str,
        title: str,
        max_approvals: int,
        initial_usage: int,
    ) -> dict[str, Any]:
        key = self.policy_key(channel_id, link)
        existing = self.data.setdefault("policies", {}).get(key)
        if existing:
            existing.update(
                {
                    "title": title,
                    "max_approvals": int(max_approvals),
                    "enabled": int(max_approvals) > 0,
                    "updated_at": utc_now_iso(),
                }
            )
            policy = existing
        else:
            policy = {
                "channel_id": int(channel_id),
                "link": link,
                "title": title,
                "max_approvals": int(max_approvals),
                "approved_by_bot": 0,
                "initial_usage": int(initial_usage),
                "enabled": int(max_approvals) > 0,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
                "last_error": None,
            }
            self.data["policies"][key] = policy
        await self.save("Set automatic approval policy")
        return policy

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

    def as_store_item(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "title": self.title,
            "username": self.username,
            "added_at": utc_now_iso(),
        }


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
    requested_hint: int
    expire_date: int | None
    pending: int | None = None

    @property
    def expired(self) -> bool:
        return bool(self.expire_date and self.expire_date <= int(time.time()))

    @property
    def active(self) -> bool:
        return not self.revoked and not self.expired


ProgressCallback = Callable[[int, int, int], Awaitable[None]]


class TelegramJoinService:
    def __init__(
        self,
        user_client: TelegramClient,
        bot_client: TelegramClient,
        store: Store,
        approval_concurrency: int,
    ) -> None:
        self.user = user_client
        self.bot = bot_client
        self.store = store
        self.approval_concurrency = approval_concurrency
        self.channel_cache: dict[int, ChannelInfo] = {}
        self.approval_locks: dict[str, asyncio.Lock] = {}
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

        raise ValueError(
            "کانال در اکانت اکانت متصل پیدا نشد. اکانتی که Session با آن ساخته شده باید داخل کانال و مدیر باشد."
        )

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

    @staticmethod
    def _admin_rights(entity: Any) -> Any | None:
        return getattr(entity, "admin_rights", None)

    @classmethod
    def _has_admin_access(cls, entity: Any) -> bool:
        # Telegram exposes the current user's creator/admin rights directly on Channel.
        # Do not use messages.getAdminsWithInvites as an admin probe: that RPC is about
        # invite statistics and can fail independently of the basic admin state.
        return bool(getattr(entity, "creator", False) or cls._admin_rights(entity))

    @classmethod
    def _can_manage_invites(cls, entity: Any) -> bool:
        if bool(getattr(entity, "creator", False)):
            return True
        rights = cls._admin_rights(entity)
        return bool(rights and getattr(rights, "invite_users", False))

    async def _refresh_channel_entity(self, channel: ChannelInfo) -> ChannelInfo:
        """Refresh the Channel object so admin_rights are not taken from stale dialog cache."""
        try:
            full = await self.user(
                functions.channels.GetFullChannelRequest(channel=channel.entity)
            )
            fresh = next(
                (chat for chat in getattr(full, "chats", []) if isinstance(chat, types.Channel) and int(chat.id) == int(channel.entity.id)),
                None,
            )
            if fresh is not None:
                return await self._channel_info_from_entity(fresh)
        except RPCError:
            pass
        return channel

    async def verify_admin_access(self, channel: ChannelInfo) -> ChannelInfo:
        channel = await self._refresh_channel_entity(channel)
        rights = self._admin_rights(channel.entity)
        log.info(
            "Channel admin check channel=%s user=%s creator=%s invite_users=%s rights_present=%s",
            channel.id,
            await self.self_user_id(),
            bool(getattr(channel.entity, "creator", False)),
            bool(rights and getattr(rights, "invite_users", False)),
            bool(rights),
        )
        if not self._has_admin_access(channel.entity):
            raise ValueError(
                "اکانت متصل در اطلاعات خود تلگرام به‌عنوان ادمین این کانال شناخته نشد. "
                "مطمئن شو همان اکانت QR شده داخل همین کانال Admin است."
            )
        if not self._can_manage_invites(channel.entity):
            raise ValueError(
                "اکانت متصل ادمین است ولی دسترسی Invite Users / Add Subscribers ندارد."
            )
        return channel

    async def register_channel(self, ref: int | str) -> ChannelInfo:
        channel = await self.resolve_channel(ref)
        channel = await self.verify_admin_access(channel)
        await self.store.upsert_channel(channel.as_store_item(), make_active=True)
        return channel

    async def scan_manageable_channels(self) -> list[ChannelInfo]:
        """Find channels from the account's dialogs using Channel.admin_rights."""
        found: dict[int, ChannelInfo] = {}
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, types.Channel):
                continue
            if not self._has_admin_access(entity) or not self._can_manage_invites(entity):
                continue
            try:
                channel = await self._channel_info_from_entity(entity, dialog.input_entity)
            except ValueError:
                continue
            found[channel.id] = channel
        return sorted(found.values(), key=lambda item: item.title.casefold())

    async def member_count(self, channel_id: int) -> int:
        channel = await self.resolve_channel(channel_id)
        try:
            result = await self.user(
                functions.channels.GetFullChannelRequest(channel=channel.input_peer)
            )
            value = getattr(result.full_chat, "participants_count", None)
            if value is not None:
                return int(value)
        except RPCError:
            pass
        participants = await self.user.get_participants(channel.input_peer, limit=0)
        return int(getattr(participants, "total", 0) or 0)

    async def _invite_admin_users(self, channel: ChannelInfo) -> list[tuple[Any, str, int]]:
        """Return admins without relying on messages.getAdminsWithInvites.

        We always include the connected account first. Then we try ChannelParticipantsAdmins
        so links created by other admins can also be discovered.
        """
        me = await self.user.get_me()
        admins: dict[int, tuple[Any, str, int]] = {}

        def add_user(user: Any, fallback_name: str = "ادمین") -> None:
            try:
                input_user = utils.get_input_user(user)
            except TypeError:
                return
            uid = int(user.id)
            name = " ".join(
                part
                for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)]
                if part
            ).strip() or getattr(user, "username", None) or fallback_name or str(uid)
            admins[uid] = (input_user, name, uid)

        add_user(me, "اکانت متصل")

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
            # Self-created links can still be queried. Keep the dashboard usable even if
            # Telegram refuses the secondary admin enumeration RPC for this channel.
            log.warning(
                "Could not enumerate all admins for channel %s: %s",
                channel.id,
                type(exc).__name__,
            )

        return list(admins.values())

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
                    if not isinstance(invite, types.ChatInviteExported):
                        continue
                    title = str(getattr(invite, "title", None) or "لینک بدون عنوان")
                    link = str(invite.link)
                    all_invites[link] = InviteInfo(
                        link=link,
                        key=short_hash(link),
                        title=title,
                        admin_id=int(invite.admin_id),
                        admin_name=admin_name,
                        request_needed=bool(getattr(invite, "request_needed", False)),
                        revoked=bool(getattr(invite, "revoked", False)),
                        permanent=bool(getattr(invite, "permanent", False)),
                        usage=int(getattr(invite, "usage", 0) or 0),
                        requested_hint=int(getattr(invite, "requested", 0) or 0),
                        expire_date=unix_timestamp(getattr(invite, "expire_date", None)),
                    )

                if len(page) < 100:
                    break
                last = page[-1]
                next_offset_key = (unix_timestamp(last.date), str(last.link))
                if next_offset_key in seen_offsets:
                    break
                seen_offsets.add(next_offset_key)
                offset_date, offset_link = last.date, str(last.link)

        return sorted(
            all_invites.values(),
            key=lambda item: (not item.active, not item.request_needed, item.title.casefold()),
        )

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

    async def enrich_pending_counts(
        self, channel_id: int, invites: list[InviteInfo]
    ) -> list[InviteInfo]:
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

    async def get_dashboard(self, channel_id: int) -> tuple[int, list[InviteInfo]]:
        count_task = asyncio.create_task(self.member_count(channel_id))
        invites_task = asyncio.create_task(self.list_invites(channel_id))
        members, invites = await asyncio.gather(count_task, invites_task)
        await self.enrich_pending_counts(channel_id, invites)
        return members, invites

    async def find_invite(self, channel_id: int, invite_key: str) -> InviteInfo | None:
        invites = await self.list_invites(channel_id)
        for invite in invites:
            if invite.key == invite_key:
                if invite.request_needed and invite.active:
                    try:
                        invite.pending = await self.pending_count(channel_id, invite.link)
                    except RPCError:
                        invite.pending = invite.requested_hint
                else:
                    invite.pending = 0
                return invite
        return None

    async def create_approval_invite(
        self, channel_id: int, title: str
    ) -> InviteInfo:
        channel = await self.resolve_channel(channel_id)
        result = await self.user(
            functions.messages.ExportChatInviteRequest(
                peer=channel.input_peer,
                request_needed=True,
                title=title[:32],
            )
        )
        if not isinstance(result, types.ChatInviteExported):
            raise RuntimeError("تلگرام لینک خروجی قابل استفاده برنگرداند.")
        return InviteInfo(
            link=str(result.link),
            key=short_hash(str(result.link)),
            title=str(getattr(result, "title", None) or title or "لینک تأییدی"),
            admin_id=int(result.admin_id),
            admin_name="اکانت مدیر",
            request_needed=bool(getattr(result, "request_needed", False)),
            revoked=bool(getattr(result, "revoked", False)),
            permanent=bool(getattr(result, "permanent", False)),
            usage=int(getattr(result, "usage", 0) or 0),
            requested_hint=int(getattr(result, "requested", 0) or 0),
            expire_date=unix_timestamp(getattr(result, "expire_date", None)),
            pending=0,
        )

    async def fetch_pending_users(self, channel_id: int, link: str) -> list[Any]:
        channel = await self.resolve_channel(channel_id)
        offset_date = epoch_datetime()
        offset_user: Any = types.InputUserEmpty()
        collected: dict[int, Any] = {}
        page_size = 100

        while True:
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
            users_by_id = {int(user.id): user for user in result.users}
            for importer in result.importers:
                user = users_by_id.get(int(importer.user_id))
                if user is None:
                    continue
                try:
                    collected[int(user.id)] = utils.get_input_user(user)
                except TypeError:
                    log.warning("Could not convert user %s to InputUser", importer.user_id)

            if len(result.importers) < page_size:
                break
            last = result.importers[-1]
            last_user = users_by_id.get(int(last.user_id))
            if last_user is None:
                break
            offset_date = last.date
            offset_user = utils.get_input_user(last_user)
            if len(collected) >= int(result.count):
                break

        return list(collected.values())

    def _approval_lock(self, channel_id: int, link: str) -> asyncio.Lock:
        key = f"{channel_id}:{link}"
        if key not in self.approval_locks:
            self.approval_locks[key] = asyncio.Lock()
        return self.approval_locks[key]

    async def approve_random(
        self,
        channel_id: int,
        link: str,
        amount: int,
        progress: ProgressCallback | None = None,
    ) -> tuple[int, int, list[str]]:
        if amount <= 0:
            raise ValueError("تعداد باید بیشتر از صفر باشد.")
        async with self._approval_lock(channel_id, link):
            channel = await self.resolve_channel(channel_id)
            pending = await self.fetch_pending_users(channel_id, link)
            if not pending:
                return 0, 0, []
            selected = random.SystemRandom().sample(pending, min(amount, len(pending)))
            semaphore = asyncio.Semaphore(self.approval_concurrency)
            done = 0
            success = 0
            errors: list[str] = []
            counter_lock = asyncio.Lock()

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

    async def approve_all(self, channel_id: int, link: str) -> None:
        async with self._approval_lock(channel_id, link):
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

    def __init__(
        self,
        bot: TelegramClient,
        service: TelegramJoinService,
        store: Store,
        settings: Settings,
        vault: AuthVault,
    ) -> None:
        self.bot = bot
        self.service = service
        self.store = store
        self.settings = settings
        self.vault = vault
        self.reauth_requested = False
        self.states: dict[int, dict[str, Any]] = {}
        self.invite_cache: dict[tuple[int, str], InviteInfo] = {}
        self.auto_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self.bot.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
        self.auto_task = asyncio.create_task(self.auto_approval_loop())

    def is_owner(self, sender_id: int | None) -> bool:
        return sender_id == self.settings.owner_id

    def set_state(self, user_id: int, name: str, **payload: Any) -> None:
        self.states[user_id] = {"name": name, **payload}

    def pop_state(self, user_id: int) -> dict[str, Any] | None:
        return self.states.pop(user_id, None)

    def active_channel(self) -> dict[str, Any] | None:
        channel_id = self.store.active_channel_id
        return self.store.get_channel(channel_id) if channel_id is not None else None

    @staticmethod
    def numeric_state(state: dict[str, Any] | None) -> bool:
        return bool(state and state.get("name") in {"approve_custom", "create_limit", "set_auto_limit"})

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
        except Exception:  # noqa: BLE001
            log.exception("Numeric OCR failed")
            return None

    def onboarding_buttons(self) -> list[list[Button]]:
        return [
            [Button.inline("🔎 شناسایی کانال‌های قابل مدیریت", b"scan_channels")],
            [Button.inline("🆔 ارسال آیدی کانال", b"ask_channel")],
            [Button.inline("ℹ️ راهنمای اتصال", b"setup_help")],
        ]

    def dashboard_footer(self, page: int = 0) -> list[list[Button]]:
        return [
            [
                Button.inline("➕ ساخت لینک تأییدی", b"create_link"),
                Button.inline("🔄 بروزرسانی", f"dashboard:{page}".encode()),
            ],
            [
                Button.inline("📺 کانال‌ها", b"channels"),
                Button.inline("ℹ️ راهنما", b"help"),
            ],
            [Button.inline("🔄 تغییر اکانت متصل", b"reauth")],
        ]

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
                buttons=[[
                    Button.inline("✅ بله، تغییر اکانت", b"reauth_confirm"),
                    Button.inline("❌ لغو", b"reauth_cancel"),
                ]],
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
                await event.respond(
                    "🔎 نتونستم عدد واضحی از تصویر بخونم. یک عکس واضح‌تر بفرست یا عدد را به‌صورت متن ارسال کن."
                )
                return
            original_state = dict(state)
            self.set_state(
                event.sender_id,
                "ocr_confirm",
                detected=detected,
                original_state=original_state,
            )
            await event.respond(
                f"🔎 از تصویر عدد **{fmt_num(detected)}** را تشخیص دادم. همین عدد استفاده شود؟",
                buttons=[
                    [Button.inline(f"✅ بله، {fmt_num(detected)}", b"ocr_confirm")],
                    [Button.inline("🔄 دوباره می‌فرستم", b"ocr_retry")],
                ],
            )
            return

        if state:
            handled = await self.handle_state_message(event, state, text)
            if handled:
                return

        if NUMBER_RE.fullmatch(text):
            await self.register_channel_from_ref(event, int(text))
            return

        if text.startswith("@"):
            await self.register_channel_from_ref(event, text)
            return

        await event.respond(
            "دستور را نشناختم. `/start` را بزن یا یک پیام از کانال فوروارد کن.",
            buttons=[[Button.inline("🏠 منو", b"home")]],
        )

    @staticmethod
    def extract_forwarded_channel_id(message: Any) -> int | None:
        header = getattr(message, "fwd_from", None)
        if header is None:
            return None
        for peer in [getattr(header, "from_id", None), getattr(header, "saved_from_peer", None)]:
            if isinstance(peer, types.PeerChannel):
                return marked_peer_id(peer)
        return None

    async def handle_state_message(
        self, event: events.NewMessage.Event, state: dict[str, Any], text: str
    ) -> bool:
        name = state.get("name")
        if name == "ocr_confirm":
            await event.respond("برای استفاده از عدد تشخیص‌داده‌شده دکمه تأیید را بزن، یا /cancel.")
            return True

        if name == "await_channel":
            if not (NUMBER_RE.fullmatch(text) or text.startswith("@")):
                await event.respond("آیدی عددی مثل `-1001234567890`، یوزرنیم یا یک پیام فورواردشده از کانال بفرست.")
                return True
            self.pop_state(event.sender_id)
            await self.register_channel_from_ref(event, int(text) if NUMBER_RE.fullmatch(text) else text)
            return True

        if name == "approve_custom":
            if not text.isdigit() or int(text) <= 0:
                await event.respond("فقط یک عدد بیشتر از صفر بفرست؛ مثال: `20`")
                return True
            self.pop_state(event.sender_id)
            await self.run_random_approval(event, state["invite_key"], int(text))
            return True

        if name == "create_title":
            title = text.strip()
            if title in {"-", "بدون نام"}:
                title = f"لینک {datetime.now().strftime('%m-%d %H:%M')}"
            if not title:
                await event.respond("یک عنوان بفرست یا `-` را برای عنوان خودکار ارسال کن.")
                return True
            self.set_state(event.sender_id, "create_limit", title=title[:32])
            await event.respond(
                "حداکثر چند نفر از درخواست‌های این لینک به‌صورت خودکار تأیید شوند؟\n\n"
                "مثلاً `20` بفرست. می‌توانی تصویر واضح عدد را هم ارسال کنی. عدد `0` یعنی فقط لینک ساخته شود و تأیید خودکار خاموش باشد."
            )
            return True

        if name == "create_limit":
            if not text.isdigit():
                await event.respond("فقط عدد بفرست؛ مثال: `20` یا `0`.")
                return True
            self.pop_state(event.sender_id)
            await self.create_new_link(event, state["title"], int(text))
            return True

        if name == "set_auto_limit":
            if not text.isdigit():
                await event.respond("فقط عدد بفرست. `0` یعنی خاموش‌کردن تأیید خودکار.")
                return True
            self.pop_state(event.sender_id)
            await self.set_auto_limit(event, state["invite_key"], int(text))
            return True

        return False

    async def on_callback(self, event: events.CallbackQuery.Event) -> None:
        if not self.is_owner(event.sender_id):
            await event.answer("دسترسی نداری", alert=True)
            return
        data = event.data.decode("utf-8", errors="ignore")
        await event.answer()

        if data == "reauth":
            await event.respond(
                "⚠️ Session فعلی پاک شود و اکانت دیگری با QR متصل شود؟",
                buttons=[[
                    Button.inline("✅ بله، تغییر اکانت", b"reauth_confirm"),
                    Button.inline("❌ لغو", b"reauth_cancel"),
                ]],
            )
        elif data == "reauth_cancel":
            await event.respond("تغییر اکانت لغو شد.", buttons=[[Button.inline("🏠 منو", b"home")]])
        elif data == "reauth_confirm":
            await event.respond("⏳ در حال پاک‌کردن Session قبلی و بازکردن QR Setup…")
            await self.vault.clear()
            self.reauth_requested = True
            await self.bot.disconnect()
        elif data == "ocr_confirm":
            state = self.states.get(event.sender_id)
            if not state or state.get("name") != "ocr_confirm":
                await event.respond("این تشخیص منقضی شده. دوباره عدد یا تصویر را بفرست.")
                return
            original = dict(state.get("original_state") or {})
            detected = int(state.get("detected", 0))
            if not original or detected < 0:
                self.pop_state(event.sender_id)
                await event.respond("ورودی OCR معتبر نیست. دوباره تلاش کن.")
                return
            self.states[event.sender_id] = original
            await self.handle_state_message(event, original, str(detected))
        elif data == "ocr_retry":
            state = self.states.get(event.sender_id)
            if state and state.get("name") == "ocr_confirm":
                original = dict(state.get("original_state") or {})
                if original:
                    self.states[event.sender_id] = original
            await event.respond("باشه؛ عکس واضح‌تر یا عدد را به‌صورت متن بفرست.")
        elif data in {"home", "dashboard"}:
            self.pop_state(event.sender_id)
            await self.edit_home(event)
        elif data.startswith("dashboard:"):
            page = int(data.split(":", 1)[1])
            await self.render_dashboard(event, edit=True, page=page)
        elif data == "scan_channels":
            await self.scan_channels(event)
        elif data == "ask_channel":
            self.set_state(event.sender_id, "await_channel")
            await event.respond(
                "آیدی عددی کانال را بفرست یا یک پیام از همان کانال برای من فوروارد کن.\n\n"
                "نمونه آیدی: `-1001234567890`\nبرای لغو: /cancel"
            )
        elif data == "setup_help":
            await event.edit(self.setup_help_text(), buttons=[[Button.inline("🔙 برگشت", b"home")]])
        elif data == "help":
            await event.edit(self.help_text(), buttons=[[Button.inline("🔙 برگشت", b"home")]])
        elif data == "channels":
            await self.render_channels(event)
        elif data.startswith("select_channel:"):
            key = data.split(":", 1)[1]
            item = self.store.get_channel_by_key(key)
            if not item:
                await event.edit("کانال پیدا نشد.", buttons=[[Button.inline("🔙 کانال‌ها", b"channels")]])
                return
            await self.store.set_active_channel(int(item["id"]))
            await self.render_dashboard(event, edit=True, page=0)
        elif data.startswith("remove_channel:"):
            key = data.split(":", 1)[1]
            item = self.store.get_channel_by_key(key)
            if item:
                await self.store.remove_channel(int(item["id"]))
            await self.render_channels(event)
        elif data.startswith("invite:"):
            await self.render_invite(event, data.split(":", 1)[1])
        elif data.startswith("approve:"):
            _, invite_key, amount = data.split(":", 2)
            if amount == "custom":
                self.set_state(event.sender_id, "approve_custom", invite_key=invite_key)
                await event.respond("چه تعداد به‌صورت تصادفی تأیید شود؟ عدد را به‌صورت متن بفرست یا یک تصویر واضح که فقط عدد داخلش باشد. مثال: `20`")
            elif amount == "all":
                await self.run_approve_all(event, invite_key)
            else:
                await self.run_random_approval(event, invite_key, int(amount))
        elif data == "create_link":
            if not self.active_channel():
                await event.edit("اول یک کانال انتخاب کن.", buttons=self.onboarding_buttons())
                return
            self.set_state(event.sender_id, "create_title")
            await event.respond(
                "برای لینک جدید یک عنوان مدیریتی بفرست. این عنوان فقط برای ادمین‌ها دیده می‌شود.\n"
                "برای عنوان خودکار `-` بفرست."
            )
        elif data.startswith("auto_limit:"):
            invite_key = data.split(":", 1)[1]
            self.set_state(event.sender_id, "set_auto_limit", invite_key=invite_key)
            await event.respond(
                "سقف کل تأیید خودکار این لینک را بفرست.\n"
                "مثال: `100` — می‌توانی عکس واضح عدد را هم بفرستی. عدد `0` یعنی تأیید خودکار خاموش شود."
            )
        elif data.startswith("disable_auto:"):
            invite = await self.get_invite(data.split(":", 1)[1])
            channel = self.active_channel()
            if invite and channel:
                await self.store.disable_policy(int(channel["id"]), invite.link)
            await self.render_invite(event, data.split(":", 1)[1])

    async def send_home(self, event: Any) -> None:
        if not self.store.channels():
            await event.respond(self.onboarding_text(), buttons=self.onboarding_buttons())
            return
        if not self.active_channel():
            await event.respond("یک کانال را انتخاب کن.", buttons=self.channel_buttons())
            return
        await self.render_dashboard(event, edit=False, page=0)

    async def edit_home(self, event: Any) -> None:
        if not self.store.channels():
            await event.edit(self.onboarding_text(), buttons=self.onboarding_buttons())
            return
        if not self.active_channel():
            await self.render_channels(event)
            return
        await self.render_dashboard(event, edit=True, page=0)

    def onboarding_text(self) -> str:
        return (
            "🤖 **مدیریت درخواست‌های عضویت**\n\n"
            "1. من را در کانال ادمین کن.\n"
            "2. اکانتی که `اکانت متصل` با آن ساخته شده هم باید ادمین همان کانال باشد.\n"
            "3. بعد یکی از این کارها را انجام بده:\n"
            "• دکمه شناسایی کانال‌ها را بزن\n"
            "• آیدی عددی کانال را بفرست\n"
            "• یک پیام از کانال برای من فوروارد کن"
        )

    async def register_channel_from_ref(self, event: Any, ref: int | str) -> None:
        status = await event.respond("⏳ در حال شناسایی و بررسی دسترسی کانال…")
        try:
            channel = await self.service.register_channel(ref)
            await status.edit(
                f"✅ کانال فعال شد.\n\n"
                f"نام: **{channel.title}**\n"
                f"آیدی: `{channel.id}`\n\n"
                "از این به بعد `/start` مستقیماً آمار همین کانال را باز می‌کند.",
                buttons=[[Button.inline("📊 مشاهده گزارش کامل", b"dashboard")]],
            )
        except Exception as exc:  # noqa: BLE001
            await status.edit(
                f"❌ کانال قابل فعال‌سازی نبود.\n`{type(exc).__name__}: {exc}`",
                buttons=[[Button.inline("🔙 راه اتصال", b"setup_help")]],
            )

    async def scan_channels(self, event: Any) -> None:
        await event.edit("⏳ در حال شناسایی کانال‌های قابل مدیریت اکانت متصل…")
        try:
            channels = await self.service.scan_manageable_channels()
        except Exception as exc:  # noqa: BLE001
            await event.edit(
                f"❌ اسکن انجام نشد: `{type(exc).__name__}: {exc}`",
                buttons=[[Button.inline("🔙 برگشت", b"home")]],
            )
            return
        if not channels:
            await event.edit(
                "کانال قابل مدیریت پیدا نشد. اکانت اکانت متصل را ادمین کانال کن، سپس دوباره اسکن کن؛ یا آیدی/فوروارد بفرست.",
                buttons=self.onboarding_buttons(),
            )
            return
        rows: list[list[Button]] = []
        for channel in channels:
            await self.store.upsert_channel(channel.as_store_item(), make_active=False)
            key = short_hash(str(channel.id), 10)
            rows.append([Button.inline(f"📺 {compact_title(channel.title, str(channel.id))}", f"select_channel:{key}".encode())])
        rows.append([Button.inline("🔙 برگشت", b"home")])
        await event.edit("کانال موردنظر را انتخاب کن:", buttons=rows)

    def channel_buttons(self) -> list[list[Button]]:
        rows: list[list[Button]] = []
        active_id = self.store.active_channel_id
        for item in self.store.channels():
            key = short_hash(str(item["id"]), 10)
            prefix = "✅" if int(item["id"]) == active_id else "📺"
            rows.append(
                [Button.inline(f"{prefix} {compact_title(item.get('title'), str(item['id']))}", f"select_channel:{key}".encode())]
            )
        rows.extend(
            [
                [Button.inline("🔎 اسکن کانال‌های قابل مدیریت", b"scan_channels")],
                [Button.inline("➕ افزودن با آیدی یا فوروارد", b"ask_channel")],
                [Button.inline("🔙 منو", b"home")],
            ]
        )
        return rows

    async def render_channels(self, event: Any) -> None:
        active = self.active_channel()
        text = "📺 **کانال‌های ثبت‌شده**"
        if active:
            text += f"\n\nکانال فعال: **{active.get('title')}**\n`{active.get('id')}`"
        await event.edit(text, buttons=self.channel_buttons())

    async def render_dashboard(self, event: Any, edit: bool, page: int) -> None:
        channel = self.active_channel()
        if not channel:
            target = event.edit if edit else event.respond
            await target(self.onboarding_text(), buttons=self.onboarding_buttons())
            return
        loading = "⏳ در حال دریافت تعداد اعضا، لینک‌ها و صف درخواست‌ها…"
        if edit:
            await event.edit(loading)
            target = event
        else:
            target = await event.respond(loading)

        try:
            members, invites = await self.service.get_dashboard(int(channel["id"]))
        except Exception as exc:  # noqa: BLE001
            await target.edit(
                f"❌ دریافت گزارش شکست خورد.\n`{type(exc).__name__}: {exc}`",
                buttons=[[Button.inline("🔄 تلاش دوباره", b"dashboard"), Button.inline("📺 کانال‌ها", b"channels")]],
            )
            return

        for invite in invites:
            self.invite_cache[(int(channel["id"]), invite.key)] = invite

        active_invites = [item for item in invites if item.active]
        approval_invites = [item for item in active_invites if item.request_needed]
        total_pending = sum(int(item.pending or 0) for item in approval_invites)
        total_pages = max(1, (len(active_invites) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * self.PAGE_SIZE
        shown = active_invites[start : start + self.PAGE_SIZE]

        lines = [
            f"📊 **{channel.get('title', 'کانال')}**",
            f"👥 اعضا: **{fmt_num(members)} نفر**",
            f"🔗 لینک‌های خصوصی فعال: **{fmt_num(len(active_invites))}**",
            f"🛂 لینک‌های دارای تأیید مدیر: **{fmt_num(len(approval_invites))}**",
            f"⏳ مجموع درخواست‌های معلق: **{fmt_num(total_pending)} نفر**",
            "",
            "یک لینک را انتخاب کن:",
        ]
        rows: list[list[Button]] = []
        for invite in shown:
            title = compact_title(invite.title, "لینک")
            if invite.request_needed:
                label = f"🛂 {title} • {fmt_num(invite.pending)} درخواست"
            else:
                label = f"🔗 {title} • ورود مستقیم"
            rows.append([Button.inline(label, f"invite:{invite.key}".encode())])

        if not shown:
            lines.append("هنوز لینک خصوصی فعالی برای این کانال پیدا نشد.")

        if total_pages > 1:
            nav: list[Button] = []
            if page > 0:
                nav.append(Button.inline("◀️ قبلی", f"dashboard:{page - 1}".encode()))
            nav.append(Button.inline(f"{page + 1}/{total_pages}", b"noop"))
            if page + 1 < total_pages:
                nav.append(Button.inline("بعدی ▶️", f"dashboard:{page + 1}".encode()))
            rows.append(nav)
        rows.extend(self.dashboard_footer(page))
        await target.edit("\n".join(lines), buttons=rows)

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
            await event.edit("کانال فعال نیست.", buttons=[[Button.inline("🔙 منو", b"home")]])
            return
        try:
            invite = await self.get_invite(invite_key)
        except Exception as exc:  # noqa: BLE001
            await event.edit(
                f"❌ خطا: `{type(exc).__name__}: {exc}`",
                buttons=[[Button.inline("🔙 گزارش", b"dashboard")]],
            )
            return
        if not invite:
            await event.edit("این لینک پیدا نشد یا حذف شده است.", buttons=[[Button.inline("🔙 گزارش", b"dashboard")]])
            return

        policy = self.store.get_policy(int(channel["id"]), invite.link)
        lines = [
            f"🔗 **{invite.title}**",
            f"`{invite.link}`",
            "",
            f"نوع ورود: **{'تأیید مدیر' if invite.request_needed else 'ورود مستقیم'}**",
            f"درخواست معلق: **{fmt_num(invite.pending)} نفر**",
            f"عضوشده با این لینک: **{fmt_num(invite.usage)} نفر**",
            f"سازنده: **{invite.admin_name}**",
        ]
        if policy:
            consumed = self.policy_consumed(policy, invite)
            maximum = int(policy.get("max_approvals", 0))
            remaining = max(0, maximum - consumed)
            status = "فعال" if policy.get("enabled") and remaining > 0 else "خاموش/تکمیل"
            lines.extend(
                [
                    "",
                    f"🤖 تأیید خودکار: **{status}**",
                    f"سقف: **{fmt_num(maximum)}** | مصرف‌شده: **{fmt_num(consumed)}** | باقی‌مانده: **{fmt_num(remaining)}**",
                ]
            )

        rows: list[list[Button]] = []
        if invite.request_needed and invite.active:
            rows.extend(
                [
                    [
                        Button.inline("✅ 10 نفر", f"approve:{invite.key}:10".encode()),
                        Button.inline("✅ 50 نفر", f"approve:{invite.key}:50".encode()),
                    ],
                    [
                        Button.inline("✅ 100 نفر", f"approve:{invite.key}:100".encode()),
                        Button.inline("🔢 تعداد دلخواه", f"approve:{invite.key}:custom".encode()),
                    ],
                    [Button.inline("⚡ تأیید همه درخواست‌ها", f"approve:{invite.key}:all".encode())],
                    [Button.inline("🤖 تنظیم سقف تأیید خودکار", f"auto_limit:{invite.key}".encode())],
                ]
            )
            if policy and policy.get("enabled"):
                rows.append([Button.inline("⏸ توقف تأیید خودکار", f"disable_auto:{invite.key}".encode())])
        rows.append([Button.inline("🔄 بروزرسانی", f"invite:{invite.key}".encode()), Button.inline("🔙 گزارش", b"dashboard")])
        await event.edit("\n".join(lines), buttons=rows)

    async def create_new_link(self, event: Any, title: str, max_approvals: int) -> None:
        channel = self.active_channel()
        if not channel:
            await event.respond("کانال فعال نیست.")
            return
        msg = await event.respond("⏳ در حال ساخت لینک اختصاصی دارای تأیید مدیر…")
        try:
            invite = await self.service.create_approval_invite(int(channel["id"]), title)
            self.invite_cache[(int(channel["id"]), invite.key)] = invite
            if max_approvals > 0:
                await self.store.set_policy(
                    int(channel["id"]),
                    invite.link,
                    invite.title,
                    max_approvals,
                    invite.usage,
                )
            await msg.edit(
                f"✅ لینک ساخته شد.\n\n"
                f"عنوان: **{invite.title}**\n"
                f"لینک: `{invite.link}`\n"
                f"تأیید مدیر: **فعال**\n"
                f"سقف تأیید خودکار: **{fmt_num(max_approvals) if max_approvals else 'خاموش'}**",
                buttons=[[Button.inline("مدیریت همین لینک", f"invite:{invite.key}".encode()), Button.inline("🏠 گزارش", b"dashboard")]],
            )
        except Exception as exc:  # noqa: BLE001
            await msg.edit(f"❌ ساخت لینک شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def set_auto_limit(self, event: Any, invite_key: str, maximum: int) -> None:
        channel = self.active_channel()
        if not channel:
            await event.respond("کانال فعال نیست.")
            return
        msg = await event.respond("⏳ در حال ذخیره تنظیمات تأیید خودکار…")
        try:
            invite = await self.get_invite(invite_key)
            if not invite or not invite.request_needed:
                raise ValueError("این لینک دارای تأیید مدیر نیست.")
            if maximum == 0:
                existing = self.store.get_policy(int(channel["id"]), invite.link)
                if existing:
                    await self.store.disable_policy(int(channel["id"]), invite.link)
                await msg.edit(
                    "⏸ تأیید خودکار این لینک خاموش شد.",
                    buttons=[[Button.inline("بازگشت به لینک", f"invite:{invite.key}".encode())]],
                )
                return
            existing = self.store.get_policy(int(channel["id"]), invite.link)
            initial_usage = int(existing.get("initial_usage", invite.usage)) if existing else invite.usage
            await self.store.set_policy(
                int(channel["id"]), invite.link, invite.title, maximum, initial_usage
            )
            await msg.edit(
                f"✅ سقف تأیید خودکار روی **{fmt_num(maximum)} نفر** تنظیم شد.\n"
                f"ربات هر **{fmt_num(self.settings.auto_scan_seconds)} ثانیه** صف این لینک را بررسی می‌کند.",
                buttons=[[Button.inline("بازگشت به لینک", f"invite:{invite.key}".encode())]],
            )
        except Exception as exc:  # noqa: BLE001
            await msg.edit(f"❌ تنظیم انجام نشد.\n`{type(exc).__name__}: {exc}`")

    async def allowed_manual_amount(self, invite: InviteInfo, requested: int) -> tuple[int, str | None]:
        channel = self.active_channel()
        if not channel:
            return requested, None
        policy = self.store.get_policy(int(channel["id"]), invite.link)
        if not policy or not policy.get("enabled"):
            return requested, None
        consumed = self.policy_consumed(policy, invite)
        remaining = max(0, int(policy.get("max_approvals", 0)) - consumed)
        if remaining <= 0:
            return 0, "سقف این لینک قبلاً کامل شده است."
        if requested > remaining:
            return remaining, f"به‌خاطر سقف خودکار، عملیات به {fmt_num(remaining)} نفر محدود شد."
        return requested, None

    async def run_random_approval(self, event: Any, invite_key: str, amount: int) -> None:
        channel = self.active_channel()
        if not channel:
            await event.respond("کانال فعال نیست.")
            return
        try:
            invite = await self.get_invite(invite_key)
        except Exception as exc:  # noqa: BLE001
            await event.respond(f"❌ دریافت لینک شکست خورد: `{type(exc).__name__}: {exc}`")
            return
        if not invite or not invite.request_needed:
            await event.respond("این لینک قابل تأیید نیست.")
            return

        amount, cap_note = await self.allowed_manual_amount(invite, amount)
        if amount <= 0:
            await event.respond(cap_note or "تعداد مجاز صفر است.")
            return

        msg = await event.respond(
            f"⏳ در حال انتخاب تصادفی و تأیید حداکثر **{fmt_num(amount)} نفر**…"
            + (f"\n{cap_note}" if cap_note else "")
        )
        last_edit = 0.0

        async def progress(done: int, total: int, success: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if done != total and now - last_edit < 2:
                return
            last_edit = now
            try:
                await msg.edit(
                    f"⏳ پیشرفت: **{fmt_num(done)}/{fmt_num(total)}**\n"
                    f"تأیید موفق: **{fmt_num(success)}**"
                )
            except RPCError:
                pass

        try:
            success, selected, errors = await self.service.approve_random(
                int(channel["id"]), invite.link, amount, progress
            )
            if success:
                policy = self.store.get_policy(int(channel["id"]), invite.link)
                if policy:
                    await self.store.update_policy_progress(
                        int(channel["id"]), invite.link, success_delta=success
                    )
            remaining = await self.service.pending_count(int(channel["id"]), invite.link)
            error_text = ""
            if errors:
                error_text = f"\nخطاها: `{', '.join(sorted(set(errors))[:5])}`"
            await msg.edit(
                f"✅ عملیات با موفقیت تمام شد.\n\n"
                f"انتخاب‌شده: **{fmt_num(selected)}**\n"
                f"تأیید موفق: **{fmt_num(success)}**\n"
                f"درخواست باقی‌مانده: **{fmt_num(remaining)} نفر**"
                f"{error_text}\n\nکار دیگری هست؟",
                buttons=[[Button.inline("بازگشت به لینک", f"invite:{invite.key}".encode()), Button.inline("🏠 گزارش", b"dashboard")]],
            )
        except Exception as exc:  # noqa: BLE001
            await msg.edit(f"❌ عملیات شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def run_approve_all(self, event: Any, invite_key: str) -> None:
        channel = self.active_channel()
        if not channel:
            await event.respond("کانال فعال نیست.")
            return
        invite = await self.get_invite(invite_key)
        if not invite or not invite.request_needed:
            await event.respond("این لینک قابل تأیید نیست.")
            return
        policy = self.store.get_policy(int(channel["id"]), invite.link)
        if policy and policy.get("enabled"):
            consumed = self.policy_consumed(policy, invite)
            remaining_cap = max(0, int(policy.get("max_approvals", 0)) - consumed)
            pending = int(invite.pending or 0)
            if pending > remaining_cap:
                await event.respond(
                    "برای رعایت سقف این لینک، تأیید همه مجاز نیست. تعداد دلخواه را انتخاب کن یا سقف را تغییر بده."
                )
                return
        msg = await event.respond("⚡ در حال تأیید همه درخواست‌های این لینک…")
        try:
            before = await self.service.pending_count(int(channel["id"]), invite.link)
            await self.service.approve_all(int(channel["id"]), invite.link)
            after = await self.service.pending_count(int(channel["id"]), invite.link)
            success = max(0, before - after)
            if success and policy:
                await self.store.update_policy_progress(
                    int(channel["id"]), invite.link, success_delta=success
                )
            await msg.edit(
                f"✅ همه درخواست‌های قابل پردازش تأیید شدند.\n"
                f"قبل: **{fmt_num(before)}**\n"
                f"تأییدشده: **{fmt_num(success)}**\n"
                f"باقی‌مانده: **{fmt_num(after)}**\n\nکار دیگری هست؟",
                buttons=[[Button.inline("بازگشت به لینک", f"invite:{invite.key}".encode()), Button.inline("🏠 گزارش", b"dashboard")]],
            )
        except Exception as exc:  # noqa: BLE001
            await msg.edit(f"❌ عملیات شکست خورد.\n`{type(exc).__name__}: {exc}`")

    async def auto_approval_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self.run_auto_approval_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Automatic approval cycle failed")
            await asyncio.sleep(self.settings.auto_scan_seconds)

    async def run_auto_approval_cycle(self) -> None:
        policies = self.store.enabled_policies()
        if not policies:
            return
        grouped: dict[int, list[dict[str, Any]]] = {}
        for policy in policies:
            grouped.setdefault(int(policy["channel_id"]), []).append(policy)

        for channel_id, channel_policies in grouped.items():
            try:
                invites = await self.service.list_invites(channel_id)
                by_link = {invite.link: invite for invite in invites}
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not list invites for %s: %s", channel_id, exc)
                continue

            for policy in channel_policies:
                invite = by_link.get(str(policy["link"]))
                if not invite or not invite.active or not invite.request_needed:
                    await self.store.update_policy_progress(
                        channel_id,
                        str(policy["link"]),
                        enabled=False,
                        last_error="invite_missing_or_inactive",
                    )
                    continue

                consumed = self.policy_consumed(policy, invite)
                maximum = int(policy.get("max_approvals", 0))
                quota_left = max(0, maximum - consumed)
                if quota_left <= 0:
                    await self.store.update_policy_progress(
                        channel_id, invite.link, enabled=False, last_error=None
                    )
                    await self.safe_notify_owner(
                        f"🏁 سقف تأیید خودکار لینک **{invite.title}** کامل شد.\n"
                        f"سقف: **{fmt_num(maximum)} نفر**"
                    )
                    continue

                try:
                    pending = await self.service.pending_count(channel_id, invite.link)
                    amount = min(pending, quota_left, self.settings.auto_approve_batch)
                    if amount <= 0:
                        continue
                    success, selected, errors = await self.service.approve_random(
                        channel_id, invite.link, amount
                    )
                    if success:
                        updated = await self.store.update_policy_progress(
                            channel_id,
                            invite.link,
                            success_delta=success,
                            last_error=None,
                        )
                        remaining_quota = max(
                            0,
                            int(updated.get("max_approvals", 0))
                            - int(updated.get("approved_by_bot", 0)),
                        ) if updated else 0
                        channel = self.store.get_channel(channel_id)
                        await self.safe_notify_owner(
                            f"🤖 **تأیید خودکار انجام شد**\n"
                            f"کانال: **{(channel or {}).get('title', channel_id)}**\n"
                            f"لینک: **{invite.title}**\n"
                            f"انتخاب‌شده: **{fmt_num(selected)}**\n"
                            f"تأیید موفق: **{fmt_num(success)}**\n"
                            f"سهمیه باقی‌مانده: **{fmt_num(remaining_quota)}**"
                        )
                    elif errors:
                        await self.store.update_policy_progress(
                            channel_id,
                            invite.link,
                            last_error=",".join(sorted(set(errors))[:5]),
                        )
                except FloodWaitError as exc:
                    log.warning("Auto approval FloodWait %s seconds", exc.seconds)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Auto approval failed for %s", invite.link)
                    await self.store.update_policy_progress(
                        channel_id,
                        invite.link,
                        last_error=type(exc).__name__,
                    )

    async def safe_notify_owner(self, text: str) -> None:
        try:
            await self.bot.send_message(
                self.settings.owner_id,
                text,
                buttons=[[Button.inline("📊 بازکردن گزارش", b"dashboard")]],
            )
        except RPCError:
            log.warning("Could not send automatic approval notification")

    def setup_help_text(self) -> str:
        return (
            "ℹ️ **اتصال کانال**\n\n"
            "• ربات را در کانال ادمین کن.\n"
            "• اکانتی که `اکانت متصل` با آن ساخته شده نیز باید ادمین و دارای دسترسی افزودن عضو باشد.\n"
            "• سپس آیدی `-100...` را بفرست یا یک پیام عادی از کانال فوروارد کن.\n"
            "• اگر فوروارد منبع را نشان نداد، آیدی عددی را بفرست.\n\n"
            "پس از فعال‌شدن، ربات لینک‌های خصوصی ادمین‌ها را از تلگرام می‌خواند؛ تشخیص ادمین بودن مستقیماً از admin_rights کانال انجام می‌شود."
        )

    def help_text(self) -> str:
        return (
            "ℹ️ **راهنمای پنل**\n\n"
            "• `/start` گزارش کانال فعال را باز می‌کند.\n"
            "• تعداد اعضا، همه لینک‌های خصوصی و درخواست هر لینک خودکار خوانده می‌شود.\n"
            "• روی هر لینک بزن و 10، 50، 100 یا تعداد دلخواه را تصادفی تأیید کن.\n"
            "• «ساخت لینک تأییدی» یک لینک جدید با Request Admin Approval می‌سازد.\n"
            "• برای هر لینک می‌توان سقف کل تأیید خودکار گذاشت.\n"
            "• برای ورودی‌های عددی مثل تعداد تأیید یا سقف Auto Approve، می‌توانی یک عکس واضح از عدد بفرستی؛ OCR بعد از تشخیص از تو تأیید می‌گیرد.\n"
            "• OCR برای Login Code یا رمز 2FA استفاده نمی‌شود؛ اتصال اکانت با QR رسمی Telegram است.\n"
            "• `/reauth` یا دکمه «تغییر اکانت متصل» Session فعلی را پاک می‌کند و QR Setup را دوباره باز می‌کند.\n"
            "• `/cancel` ورودی عدد یا ساخت لینک نیمه‌کاره را لغو می‌کند."
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
    service = TelegramJoinService(
        user_client,
        bot_client,
        store,
        settings.approval_concurrency,
    )
    control = ControlBot(bot_client, service, store, settings, vault)
    await control.start()

    me = await bot_client.get_me()
    log.info("Control bot @%s started for owner %s", me.username, settings.owner_id)
    try:
        account_line = ""
        if settings.connected_name or settings.connected_user_id:
            account_line = (
                f"\n👤 اکانت متصل: **{settings.connected_name or 'Telegram User'}**"
                + (f" (`{settings.connected_user_id}`)" if settings.connected_user_id else "")
            )
        await bot_client.send_message(
            settings.owner_id,
            "🟢 پنل اصلی مدیریت درخواست‌ها اجرا شد." + account_line,
            buttons=[[Button.inline("بازکردن پنل", b"home")]],
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
    persist_to_git = os.getenv("PERSIST_TO_GIT", "true").lower() in {
        "1", "true", "yes", "on"
    }

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

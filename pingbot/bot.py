import asyncio
import json
import logging
import time
from weakref import WeakValueDictionary

from telethon import errors
from telethon.tl.types import User

from .mentions import ALL_PATTERN, user_batches, username_batches
from .menus import PrivateMenu
from .storage import normalize
from .stickers import StickerAdmin

LOG = logging.getLogger(__name__)


class Bot(StickerAdmin, PrivateMenu):
    def __init__(self, client, storage, config, username):
        self.client, self.storage, self.config = client, storage, config
        self.username = username.lower()
        self.locks = WeakValueDictionary()
        self.last_ping = {}
        self.blocked_until = 0.0

    def lock(self, key):
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock

    async def reply(self, event, text, buttons=None):
        return await event.reply(text, parse_mode=None, link_preview=False, buttons=buttons)

    async def handle(self, event):
        callback = isinstance(getattr(event, "data", None), bytes)
        if not event.sender_id or event.sender_id <= 0:
            return
        if time.monotonic() < self.blocked_until:
            return
        try:
            if callback:
                if not event.is_private or event.chat_id != event.sender_id:
                    await event.answer("Откройте меню в личке с ботом.", alert=True)
                    return
                await event.answer()
            sender = await event.get_sender()
            if not isinstance(sender, User) or sender.bot:
                return
            if event.is_private:
                async with self.lock(("user", event.sender_id)):
                    if callback:
                        await self.callback(event)
                    else:
                        await self.private(event)
            elif event.is_group:
                if getattr(getattr(event, 'message', None), 'action', None):
                    return
                await self.auto_sticker(event)
                if event.raw_text and time.monotonic() >= self.blocked_until:
                    await self.group(event)
        except errors.FloodWaitError as exc:
            # Do not retry sends: earlier batches may already have been delivered.
            self.blocked_until = time.monotonic() + exc.seconds
            LOG.warning("Telegram FloodWait: pause for %s seconds", exc.seconds)
        except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
            await self.safe_reply(event, "Нет доступа к участникам. Назначьте бота администратором чата и повторите запрос.")
        except errors.ChatWriteForbiddenError:
            LOG.warning("Bot cannot write in chat %s", event.chat_id)
        except errors.RPCError as exc:
            LOG.warning("Telegram request failed: %s", type(exc).__name__)
            await self.safe_reply(event, "Telegram отклонил запрос. Операция могла выполниться частично; проверьте сообщения и права бота.")
        except Exception as exc:
            LOG.error("Handler failed: %s", type(exc).__name__)
            await self.safe_reply(event, "Не удалось завершить операцию. Попробуйте ещё раз.")

    async def safe_reply(self, event, text):
        try:
            await self.reply(event, text)
        except errors.FloodWaitError as exc:
            self.blocked_until = time.monotonic() + exc.seconds
        except errors.RPCError:
            LOG.warning("Could not send error reply in chat %s", event.chat_id)

    async def group(self, event):
        text = event.raw_text.strip()
        all_requested = bool(ALL_PATTERN.search(text))
        members = None
        if not all_requested:
            normalized = normalize(text)
            # Longest matching phrase wins if two phrase/list combinations collide.
            for phrase in sorted(self.storage.phrases(event.sender_id),
                                 key=lambda p: len(p["text_key"]), reverse=True):
                prefix = phrase["text_key"] + " "
                if normalized.startswith(prefix):
                    row = self.storage.get(event.sender_id, normalized[len(prefix):])
                    if row:
                        members = json.loads(row["members"])
                        break
            if members is None:
                return
        lock = self.lock(("chat", event.chat_id))
        if lock.locked():
            return  # Do not queue multiple mass mentions in the same chat.
        async with lock:
            chat = await event.get_input_chat()
            if all_requested and not self.config.all_allow_everyone:
                permissions = await self.client.get_permissions(chat, event.sender_id)
                if not permissions or not (permissions.is_admin or permissions.is_creator):
                    await self.reply(event, "@all доступен только администраторам этого чата.")
                    return
            now = time.monotonic()
            if now - self.last_ping.get(event.chat_id, float('-inf')) < self.config.cooldown_seconds:
                return
            self.last_ping[event.chat_id] = now
            if all_requested:
                participants = await self.client.get_participants(chat, limit=None)
                unique_count = len({u.id for u in participants})
                total = participants.total
                if total is None or unique_count < total:
                    await self.reply(event, f"Telegram вернул неполный список участников ({unique_count} из {total if total is not None else '?'}). Упомяну доступных. Проверьте права администратора у бота; в больших чатах возможны ограничения Telegram.")
                sent = False
                for batch, entities in user_batches(participants):
                    if sent:
                        await asyncio.sleep(1)
                    await self.client.send_message(chat, batch, formatting_entities=entities,
                                                   parse_mode=None, reply_to=event.id)
                    sent = True
                if not sent:
                    await self.reply(event, "Нет доступных участников для упоминания.")
            else:
                for i, batch in enumerate(username_batches(members)):
                    if i:
                        await asyncio.sleep(1)
                    await self.reply(event, batch)
            self.last_ping[event.chat_id] = time.monotonic()

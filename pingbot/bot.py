import asyncio
import json
import logging
import time
from weakref import WeakValueDictionary

from telethon import errors
from telethon.tl.types import User

from .mentions import ALL_PATTERN, parse_members, user_batches, username_batches
from .storage import normalize

LOG = logging.getLogger(__name__)
HELP = """Личные списки участников:
/new — создать список
/lists — мои списки и фраза
/show название — показать список
/edit название — заменить участников
/delete название — удалить список
/phrase новая фраза — изменить мою кодовую фразу
/cancel — отменить ввод

В чате: я иду домой дота 2
Используется только ваш список. Фраза по умолчанию: я иду домой.
@all — упомянуть участников текущего чата.
Бот должен видеть сообщения: назначьте его администратором.
"""


class Bot:
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

    async def reply(self, event, text):
        return await event.reply(text, parse_mode=None, link_preview=False)

    async def handle(self, event):
        if not event.raw_text or not event.sender_id or event.sender_id <= 0:
            return
        if time.monotonic() < self.blocked_until:
            return
        try:
            sender = await event.get_sender()
            if not isinstance(sender, User) or sender.bot:
                return
            if event.is_private:
                async with self.lock(("user", event.sender_id)):
                    await self.private(event)
            elif event.is_group:
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

    async def private(self, event):
        owner = event.sender_id
        text = event.raw_text.strip()
        self.storage.register(owner)
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            command, _, target = command.partition("@")
            if target and target.lower() != self.username:
                return
            arg = " ".join(arg.split())
            command = command.lower()
            if command in {"/start", "/new"}:
                self.storage.set_draft(owner, "name")
                await self.reply(event, "Введите название личного списка, например: дота 2.\n/cancel — отмена, /help — помощь.")
            elif command == "/help":
                await self.reply(event, HELP)
            elif command == "/cancel":
                self.storage.cancel(owner)
                await self.reply(event, "Ввод отменён.")
            elif command == "/phrase":
                if not arg or len(arg) > 100 or ALL_PATTERN.search(arg):
                    await self.reply(event, "Формат: /phrase я иду домой\nДо 100 символов, без @all.")
                    return
                self.storage.set_phrase(owner, arg)
                await self.reply(event, f"Ваша фраза: {arg}\nВ чате напишите её и название своего списка.")
            elif command == "/lists":
                rows = self.storage.lists(owner)
                await self.reply(event, f"Ваша фраза: {self.storage.phrase(owner)}\n" +
                                 ("Ваши списки:" if rows else "Списков нет. Создать: /new"))
                for i in range(0, len(rows), 25):
                    await self.reply(event, "\n".join(f"• {r['name']} ({len(json.loads(r['members']))})" for r in rows[i:i + 25]))
            elif command in {"/show", "/edit", "/delete"}:
                row = self.storage.get(owner, arg)
                if not row:
                    await self.reply(event, "Ваш список не найден. Укажите название после команды. /lists — ваши списки.")
                elif command == "/show":
                    await self.reply(event, row["name"])
                    for batch in username_batches(json.loads(row["members"])):
                        await self.reply(event, batch)
                elif command == "/edit":
                    self.storage.set_draft(owner, "members", row["name"])
                    await self.reply(event, "Отправьте новый список @username через пробел. Старый сохранится до завершения ввода.")
                else:
                    self.storage.delete(owner, arg)
                    self.storage.cancel(owner)
                    await self.reply(event, "Список удалён.")
            else:
                await self.reply(event, "Неизвестная команда. /help — помощь.")
            return
        draft = self.storage.draft(owner)
        if not draft:
            await self.reply(event, "Создать список: /new. Изменить фразу: /phrase ваша фраза. /help — помощь.")
        elif draft["stage"] == "name":
            name = " ".join(text.split())
            if not name or len(name) > 80 or ALL_PATTERN.search(name):
                await self.reply(event, "Название должно содержать от 1 до 80 символов, без @all.")
            elif self.storage.get(owner, name):
                await self.reply(event, "У вас уже есть такой список. Выберите другое название или /edit название.")
            else:
                self.storage.set_draft(owner, "members", name)
                await self.reply(event, "Отправьте одним сообщением @username участников через пробел (до 200).")
        else:
            try:
                members = parse_members(text)
            except ValueError as exc:
                await self.reply(event, str(exc))
                return
            self.storage.save(owner, draft["name"], members)
            await self.reply(event, f"Сохранено участников: {len(members)}.\nВ чате напишите: {self.storage.phrase(owner)} {draft['name']}\nИзменить кодовую фразу: /phrase ваша фраза")

    async def group(self, event):
        text = event.raw_text.strip()
        all_requested = bool(ALL_PATTERN.search(text))
        members = None
        if not all_requested:
            phrase = self.storage.phrase(event.sender_id)
            if not phrase:
                return
            prefix = normalize(phrase) + " "
            normalized = normalize(text)
            if not normalized.startswith(prefix):
                return
            row = self.storage.get(event.sender_id, normalized[len(prefix):])
            if not row:
                return
            members = json.loads(row["members"])
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

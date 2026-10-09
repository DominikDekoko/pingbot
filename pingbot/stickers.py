import base64
import io
import json
import logging
import math
import random
import re
import sqlite3
import time

from telethon import errors
from telethon.extensions import BinaryReader
from telethon.tl.types import User

from .menus import CANCEL, PAGE_SIZE, button

LOG = logging.getLogger(__name__)
MAX_STICKER_BYTES = 2 * 1024 * 1024


def probability(text):
    try:
        value = float(text.strip().removesuffix('%').strip().replace(',', '.'))
    except ValueError:
        raise ValueError("Введите вероятность от 0 до 100, например 25 или 12,5%.") from None
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("Вероятность должна быть от 0 до 100 процентов.")
    return value


class StickerAdmin:
    def is_admin(self, owner):
        return owner in self.config.admin_ids

    async def require_admin(self, event):
        if self.is_admin(event.sender_id):
            return True
        self.storage.cancel(event.sender_id)
        await self.reply(event, "Эта настройка доступна только администраторам бота.", CANCEL)
        return False

    async def sticker_catalog(self, event, page=0):
        if not await self.require_admin(event):
            return
        self.storage.cancel(event.sender_id)
        rows = self.storage.sticker_rules()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        buttons = [[button(f"@{r['username']} · {r['chance']:g}%", f"sticker:view:{r['id']}")]
                   for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        nav = []
        if page:
            nav.append(button("← Назад", f"sticker:page:{page - 1}"))
        if page + 1 < pages:
            nav.append(button("Далее →", f"sticker:page:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons += [[button("Добавить пользователя", "sticker:add")], [button("Главное меню", "home")]]
        await self.reply(event, f"Стикер-ответы · {page + 1}/{pages}\n"
                         "Общие настройки администраторов для всех групп с ботом.\n"
                         "Выберите пользователя или добавьте нового.", buttons)

    async def sticker_detail(self, event, item_id):
        if not await self.require_admin(event):
            return
        row = self.storage.sticker_rule(item_id)
        if row is None:
            await self.reply(event, "Правило уже удалено.", CANCEL)
            return
        await self.reply(event, f"Пользователь: @{row['username']}\nTelegram ID: {row['target_id']}\n"
                         f"Вероятность: {row['chance']:g}%\n"
                         "Ответ на каждое новое сообщение во всех группах с ботом. 0% отключает ответы.", [
                             [button("Показать стикер", f"sticker:preview:{item_id}")],
                             [button("Изменить стикер", f"sticker:edit:{item_id}")],
                             [button("Изменить вероятность", f"sticker:chance:{item_id}")],
                             [button("Удалить", f"sticker:askdelete:{item_id}")],
                             [button("Назад", "sticker:page:0")]])

    async def sticker_callback(self, event, data):
        if not await self.require_admin(event):
            return
        match = re.fullmatch(r"sticker:(add|page|view|preview|edit|chance|askdelete|delete)(?::([a-f0-9]{32}|[0-9]{1,9}))?", data)
        if not match:
            await self.sticker_catalog(event)
            return
        action, item_id = match.groups()
        if action == 'page' and item_id and item_id.isdigit():
            await self.sticker_catalog(event, int(item_id))
            return
        if action == 'add':
            self.storage.set_draft(event.sender_id, 'admin_target')
            await self.reply(event, "Введите @username пользователя.", CANCEL)
            return
        row = self.storage.sticker_rule(item_id)
        if row is None:
            await self.reply(event, "Правило уже удалено или кнопка устарела.", CANCEL)
            return
        self.storage.cancel(event.sender_id)
        if action == 'view':
            await self.sticker_detail(event, item_id)
        elif action == 'preview':
            await self.send_saved_sticker(await event.get_input_chat(), row)
        elif action in {'edit', 'chance'}:
            self.storage.set_draft(event.sender_id, 'admin_' + action, json.dumps({'id': item_id}))
            await self.reply(event, "Отправьте новый стикер." if action == 'edit'
                             else "Введите вероятность от 0 до 100 процентов.", CANCEL)
        elif action == 'askdelete':
            await self.reply(event, f"Удалить стикер-ответы для @{row['username']}?", [
                [button("Да, удалить", f"sticker:delete:{item_id}")],
                [button("Нет, оставить", f"sticker:view:{item_id}")]])
        elif action == 'delete':
            self.storage.delete_sticker_rule(item_id)
            await self.sticker_catalog(event)

    async def sticker_input(self, event, draft):
        if not await self.require_admin(event):
            return
        stage = draft['stage']
        payload = json.loads(draft['name']) if draft['name'] else {}
        text = event.raw_text.strip()
        try:
            if stage == 'admin_target':
                if not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{0,31}', text):
                    raise ValueError("Введите один @username пользователя, без ссылки.")
                try:
                    user = await self.client.get_entity(text)
                except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                    raise ValueError("Пользователь не найден. Проверьте @username.") from None
                if not isinstance(user, User) or user.bot or user.deleted:
                    raise ValueError("Выберите пользователя, а не бота, группу или канал.")
                existing = self.storage.sticker_for_user(user.id)
                if existing:
                    self.storage.cancel(event.sender_id)
                    await self.sticker_detail(event, existing['id'])
                    return
                payload = {'target_id': user.id, 'username': text[1:]}
                self.storage.set_draft(event.sender_id, 'admin_sticker', json.dumps(payload))
                await self.reply(event, f"Выбран {text} (ID {user.id}). Теперь отправьте стикер.", CANCEL)
            elif stage in {'admin_sticker', 'admin_edit'}:
                document = getattr(event.message, 'sticker', None)
                if document is None:
                    raise ValueError("Отправьте именно стикер, не картинку или текст.")
                if document.size > MAX_STICKER_BYTES:
                    raise ValueError("Стикер слишком большой (максимум 2 МиБ).")
                content = await self.client.download_media(event.message, file=bytes)
                if not content or len(content) > MAX_STICKER_BYTES:
                    raise ValueError("Не удалось загрузить стикер. Отправьте другой.")
                if stage == 'admin_edit':
                    self.storage.update_sticker_rule(event.sender_id, payload['id'], document=bytes(document), content=content)
                    await self.sticker_detail(event, payload['id'])
                else:
                    payload.update(document=base64.b64encode(bytes(document)).decode(),
                                   content=base64.b64encode(content).decode())
                    self.storage.set_draft(event.sender_id, 'admin_probability', json.dumps(payload))
                    await self.reply(event, "Введите вероятность ответа от 0 до 100 процентов. Например: 25.", CANCEL)
            elif stage in {'admin_probability', 'admin_chance'}:
                chance = probability(text)
                if stage == 'admin_chance':
                    item_id = payload['id']
                    self.storage.update_sticker_rule(event.sender_id, item_id, chance=chance)
                else:
                    item_id = self.storage.save_sticker_rule(
                        event.sender_id, payload['target_id'], payload['username'], chance,
                        base64.b64decode(payload['document']), base64.b64decode(payload['content']))
                await self.sticker_detail(event, item_id)
        except sqlite3.IntegrityError:
            self.storage.cancel(event.sender_id)
            await self.reply(event, "Другой администратор уже добавил этого пользователя. Откройте его в разделе «Стикер-ответы».", CANCEL)
        except ValueError as exc:
            await self.reply(event, str(exc), CANCEL)

    async def send_saved_sticker(self, chat, row, reply_to=None):
        # Re-upload saved bytes: Telegram file_reference can expire after a restart.
        with BinaryReader(row['document']) as reader:
            document = reader.tgread_object()
        file = io.BytesIO(row['content'])
        suffix = {'image/webp': 'webp', 'application/x-tgsticker': 'tgs', 'video/webm': 'webm'}.get(document.mime_type, 'webp')
        file.name = 'sticker.' + suffix
        await self.client.send_file(chat, file, mime_type=document.mime_type,
                                    attributes=document.attributes, force_document=True, reply_to=reply_to)

    async def auto_sticker(self, event):
        row = self.storage.sticker_for_user(event.sender_id)
        if not row or row['chance'] <= 0 or random.random() * 100 >= row['chance']:
            return
        try:
            await self.send_saved_sticker(await event.get_input_chat(), row, reply_to=event.id)
        except errors.FloodWaitError as exc:
            self.blocked_until = time.monotonic() + exc.seconds
            LOG.warning("Sticker FloodWait: pause for %s seconds", exc.seconds)
        except Exception as exc:
            # Automatic reactions must not flood the group with error messages
            # or prevent a personal-list / @all command from being processed.
            LOG.warning("Sticker reply failed in chat %s: %s", event.chat_id, type(exc).__name__)

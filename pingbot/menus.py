import json
import re
import sqlite3

from telethon import Button

from .mentions import ALL_PATTERN, parse_members, username_batches


def button(text, data):
    return Button.inline(text, data.encode())


HOME = [[Button.text("Мои фразы", resize=True, persistent=True),
         Button.text("Мои списки", resize=True, persistent=True)]]
CANCEL = [[button("Отмена", "home")]]
PAGE_SIZE = 8


class PrivateMenu:
    async def home(self, event):
        self.storage.cancel(event.sender_id)
        buttons = HOME + ([[Button.text("Стикер-ответы", resize=True, persistent=True)]]
                          if self.is_admin(event.sender_id) else [])
        await self.reply(event, "Выберите раздел. Любая ваша фраза работает с любым вашим списком.\n"
                         "В чате напишите: фраза + название списка.\n"
                         "Например: я иду домой дота 2.\n@all — упомянуть участников чата.", buttons)

    async def catalog(self, event, kind, page=0):
        self.storage.cancel(event.sender_id)
        rows = self.storage.lists(event.sender_id) if kind == "list" else self.storage.phrases(event.sender_id)
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        field = "name" if kind == "list" else "text"
        buttons = [[button(row[field], f"{kind}:view:{row['id']}")]
                   for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(button("← Назад", f"{kind}:page:{page - 1}"))
        if page + 1 < pages:
            navigation.append(button("Далее →", f"{kind}:page:{page + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons += [[button("Добавить список" if kind == "list" else "Добавить фразу", f"{kind}:add")],
                    [button("Главное меню", "home")]]
        title = "Мои списки" if kind == "list" else "Мои фразы"
        await self.reply(event, f"{title} · {page + 1}/{pages}\n" +
                         ("Выберите кнопку." if rows else "Пока пусто. Нажмите «Добавить»."), buttons)

    async def detail(self, event, kind, item_id):
        row = self.storage.item(event.sender_id, kind, item_id)
        if row is None:
            await self.reply(event, "Элемент уже удалён или недоступен.", CANCEL)
            return
        if kind == "list":
            members = json.loads(row['members'])
            # Keep the detail card within Telegram's message length limit.
            for batch in username_batches(members, size=80):
                await self.reply(event, batch)
            phrases = self.storage.phrases(event.sender_id)
            example = (f"Пример вызова: {phrases[0]['text']} {row['name']}" if phrases
                       else "Добавьте кодовую фразу в разделе «Мои фразы».")
            text = f"Список: {row['name']}\nУчастников: {len(members)}\n{example}"
            buttons = [[button("Изменить участников", f"list:edit:{item_id}")],
                       [button("Переименовать", f"list:rename:{item_id}")]]
        else:
            text = f"Фраза: {row['text']}\nПодходит ко всем вашим спискам. После фразы укажите название списка."
            buttons = [[button("Изменить фразу", f"phrase:edit:{item_id}")]]
        buttons += [[button("Удалить", f"{kind}:askdelete:{item_id}")],
                    [button("Назад к спискам" if kind == "list" else "Назад к фразам", f"{kind}:page:0")],
                    [button("Главное меню", "home")]]
        await self.reply(event, text, buttons)

    async def callback(self, event):
        self.storage.register(event.sender_id)
        try:
            data = event.data.decode("ascii")
        except UnicodeDecodeError:
            return
        if data == "home":
            await self.home(event)
            return
        if data.startswith('sticker:'):
            await self.sticker_callback(event, data)
            return
        match = re.fullmatch(r"(list|phrase):(page|add|view|edit|rename|askdelete|delete)(?::([a-f0-9]{32}|[0-9]{1,9}))?", data)
        if not match:
            await self.reply(event, "Кнопка устарела. Откройте главное меню.", CANCEL)
            return
        kind, action, item_id = match.groups()
        if action == "page" and item_id and item_id.isdigit():
            await self.catalog(event, kind, int(item_id))
            return
        if action == "add":
            self.storage.set_draft(event.sender_id, "name" if kind == "list" else "phrase_add")
            await self.reply(event, "Введите название списка (до 80 символов)." if kind == "list"
                             else "Введите новую кодовую фразу (до 100 символов).", CANCEL)
            return
        row = self.storage.item(event.sender_id, kind, item_id)
        if row is None:
            await self.reply(event, "Элемент уже удалён или недоступен.", CANCEL)
            return
        self.storage.cancel(event.sender_id)
        if action == "view":
            await self.detail(event, kind, item_id)
        elif action in {"edit", "rename"}:
            if kind == "phrase":
                stage, prompt = "phrase_edit", "Введите новую кодовую фразу (до 100 символов)."
            elif action == "rename":
                stage, prompt = "list_rename", "Введите новое название списка (до 80 символов)."
            else:
                stage, prompt = "edit_members", "Отправьте новый список @username через пробел (до 200)."
            self.storage.set_draft(event.sender_id, stage, item_id)
            await self.reply(event, prompt + "\nТекущее значение сохранится до завершения ввода.", CANCEL)
        elif action == "askdelete":
            label = row['name'] if kind == 'list' else row['text']
            await self.reply(event, f"Удалить «{label}»?", [
                [button("Да, удалить", f"{kind}:delete:{item_id}")],
                [button("Нет, оставить", f"{kind}:view:{item_id}")]])
        elif action == "delete":
            self.storage.delete_item(event.sender_id, kind, item_id)
            await self.catalog(event, kind)

    async def private(self, event):
        owner = event.sender_id
        text = event.raw_text.strip()
        self.storage.register(owner)
        if text == "Стикер-ответы":
            await self.sticker_catalog(event)
            return
        if text in {"Мои фразы", "Мои списки"}:
            await self.catalog(event, "phrase" if text == "Мои фразы" else "list")
            return
        if text.startswith("/"):
            # /start remains the Telegram entry point; management uses buttons.
            command = text.split()[0]
            _, _, target = command.partition("@")
            if target and target.lower() != self.username:
                return
            await self.home(event)
            return
        draft = self.storage.draft(owner)
        if draft and draft['stage'].startswith('admin_'):
            await self.sticker_input(event, draft)
            return
        if not draft:
            await self.home(event)
            return
        text = " ".join(text.split())
        stage, target = draft['stage'], draft['name']
        try:
            if stage in {"name", "list_rename", "phrase_add", "phrase_edit"}:
                maximum = 100 if stage.startswith("phrase") else 80
                if not text or len(text) > maximum or ALL_PATTERN.search(text) or text.startswith("/"):
                    raise ValueError(f"Введите от 1 до {maximum} символов без @all и команды в начале.")
            if stage == "name":
                if self.storage.get(owner, text):
                    raise ValueError("У вас уже есть список с таким названием. Введите другое.")
                self.storage.set_draft(owner, "members", text)
                await self.reply(event, "Отправьте @username участников через пробел (до 200).", CANCEL)
                return
            if stage == "members":
                if self.storage.get(owner, target):
                    raise ValueError("Такой список уже существует. Откройте его через «Мои списки».")
                self.storage.save(owner, target, parse_members(text))
                await self.detail(event, "list", self.storage.get(owner, target)['id'])
            elif stage == "edit_members":
                self.storage.replace_members(owner, target, parse_members(text))
                await self.detail(event, "list", target)
            elif stage == "list_rename":
                self.storage.rename_list(owner, target, text)
                await self.detail(event, "list", target)
            elif stage in {"phrase_add", "phrase_edit"}:
                item_id = self.storage.save_phrase(owner, text, target if stage == "phrase_edit" else None)
                await self.detail(event, "phrase", item_id)
            else:
                await self.home(event)
        except sqlite3.IntegrityError:
            await self.reply(event, "Такое название или фраза уже есть у вас. Введите другое значение.", CANCEL)
        except ValueError as exc:
            await self.reply(event, str(exc), CANCEL)

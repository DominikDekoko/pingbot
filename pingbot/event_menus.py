"""Button-driven administration for independent condition/action pairs."""
import base64
import json
import re
from uuid import UUID

from telethon import errors, types

from .automation import ACTIONS, CONDITIONS, MEDIA, REACTIONS
from .mentions import parse_members
from .menus import button, CANCEL, PAGE_SIZE
from .stickers import probability

CATEGORIES = {'e': 'event', 'c': 'condition', 'a': 'action'}
TITLES = {'e': 'События', 'c': 'Условия', 'a': 'Действия'}


def short(item_id):
    return base64.urlsafe_b64encode(UUID(item_id).bytes).decode().rstrip('=')


def long(token):
    return UUID(bytes=base64.urlsafe_b64decode(token + '==')).hex


def integer(text, low, high):
    if not re.fullmatch(r'[0-9]+', text.strip()):
        raise ValueError(f'Введите целое число от {low} до {high}.')
    value = int(text)
    if not low <= value <= high:
        raise ValueError(f'Введите целое число от {low} до {high}.')
    return value


class EventMenus:
    async def event_home(self, event):
        if not await self.require_admin(event):
            return
        self.storage.cancel(event.sender_id)
        await self.reply(event, 'Событие — одна пара: условие → действие.\n'
                         'Создайте элементы, затем выберите их в событии. Элементы можно использовать повторно.',
                         [[button(title, f'ev:list:{cat}:0')] for cat, title in TITLES.items()] +
                         [[button('Главное меню', 'home')]])

    def event_item(self, cat, item_id):
        row = self.storage.event_rule(item_id) if cat == 'e' else self.storage.block(item_id)
        if row is None or (cat != 'e' and row['category'] != CATEGORIES[cat]):
            raise ValueError('Элемент удалён. Откройте меню заново.')
        return row

    async def event_catalog(self, event, cat, page=0, selecting=None):
        self.storage.cancel(event.sender_id)
        rows = self.storage.event_rules() if cat == 'e' else self.storage.blocks(CATEGORIES[cat])
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        chosen = {r['id'] for r in self.storage.linked_blocks(selecting)} if selecting else set()
        buttons = []
        for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            prefix = '✓ ' if row['id'] in chosen else '● ' if row['enabled'] else '○ '
            callback = f"ev:bind:{short(selecting)}:{short(row['id'])}" if selecting else f"ev:view:{cat}:{row['id']}"
            buttons.append([button(prefix + row['name'], callback)])
        nav = []
        for label, destination in [('←', page - 1), ('→', page + 1)]:
            if 0 <= destination < pages:
                callback = f'ev:pick:{cat}:{selecting}:{destination}' if selecting else f'ev:list:{cat}:{destination}'
                nav.append(button(label, callback))
        if nav:
            buttons.append(nav)
        if not selecting:
            buttons.append([button('Добавить', f'ev:new:{cat}')])
        buttons.append([button('Назад', f'ev:view:e:{selecting}' if selecting else 'ev:home')])
        await self.reply(event, f'{TITLES[cat]} · {page + 1}/{pages}\n' +
                         ('Выберите один элемент. Повторное нажатие снимает выбор. Событие выключится до повторного включения.' if selecting else
                          '● включено · ○ выключено. Изменения общего элемента влияют на все использующие его события.'), buttons)

    async def event_detail(self, event, cat, item_id):
        row = self.event_item(cat, item_id)
        status = 'включено' if row['enabled'] else 'выключено'
        buttons = [[button('Выключить' if row['enabled'] else 'Включить', f'ev:toggle:{cat}:{item_id}')]]
        text = f"{row['name']}\nСостояние: {status}"
        if cat == 'e':
            blocks = self.storage.linked_blocks(item_id)
            for category, label, code in [('condition', 'Условие', 'c'), ('action', 'Действие', 'a')]:
                names = [b['name'] + (' (выключено)' if not b['enabled'] else '') for b in blocks if b['category'] == category]
                text += f"\n{label}: {', '.join(names) or 'не выбрано'}"
                buttons.append([button('Выбрать: ' + label.lower(), f'ev:pick:{code}:{item_id}:0')])
            chats = json.loads(row['chats'])
            text += f"\nШанс: {row['chance']:g}%\nПауза в чате: {row['cooldown']} сек.\nЧаты: " + (', '.join(map(str, chats)) if chats else 'все группы')
            buttons += [[button('Вероятность', f'ev:setting:chance:{item_id}'), button('Пауза', f'ev:setting:cooldown:{item_id}')],
                        [button('Чаты', f'ev:chats:{item_id}:0')]]
        else:
            labels = CONDITIONS if cat == 'c' else ACTIONS
            data = json.loads(row['data'])
            text += '\nТип: ' + labels.get(row['kind'], row['kind'])
            if 'media' in data:
                text += '\nМедиа сохранено. Для замены нажмите «Настроить».'
            elif row['kind'] == 'legacy_sticker':
                text += '\nПеренесено из стикер-ответов. Настройки доступны в прежнем разделе.'
            else:
                details = []
                if 'words' in data:
                    details.append('Слова: ' + ', '.join(data['words']))
                if 'count' in data:
                    details.append(f"Порог: {data['count']} сообщений за {data['seconds']} сек.")
                if cat == 'c':
                    details.append('Пользователь: ' + (str(data['user_id']) if data.get('user_id') else 'любой / весь чат'))
                if 'text' in data:
                    details.append(data['text'])
                if 'emoji' in data:
                    details.append('Emoji: ' + data['emoji'])
                if 'members' in data:
                    details.append(' '.join(data['members']))
                if 'question' in data:
                    details.append(data['question'] + '\n' + '\n'.join(data['answers']))
                text += '\n' + '\n'.join(details)[:2400]
            usage = [r['name'] for r in self.storage.event_rules() if any(b['id'] == item_id for b in self.storage.linked_blocks(r['id']))]
            text += '\nИспользуется в событиях: ' + (', '.join(usage)[:500] or 'пока нет')
            if row['kind'] != 'legacy_sticker':
                buttons.append([button('Настроить', f'ev:edit:{cat}:{item_id}')])
        buttons += [[button('Переименовать', f'ev:rename:{cat}:{item_id}')],
                    [button('Удалить', f'ev:askdelete:{cat}:{item_id}')],
                    [button('Назад', f'ev:list:{cat}:0')]]
        await self.reply(event, text, buttons)

    async def event_chats(self, event, item_id, page=0):
        rule = self.event_item('e', item_id)
        selected = json.loads(rule['chats'])
        rows = self.storage.known_chats()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        buttons = [[button(('✓ ' if row['id'] in selected else '') + row['title'][:70], f"ev:chat:{short(item_id)}:{row['id']}")]
                   for row in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        nav = [button(label, f'ev:chats:{item_id}:{p}') for label, p in [('←', page - 1), ('→', page + 1)] if 0 <= p < pages]
        if nav:
            buttons.append(nav)
        buttons += [[button('Все группы', f'ev:everywhere:{item_id}')],
                    [button('Ввести ID чатов', f'ev:setting:chats:{item_id}')],
                    [button('Назад', f'ev:view:e:{item_id}')]]
        await self.reply(event, 'Выбор чатов. Пустой выбор означает все группы.\n'
                         'Список пополняется, когда бот видит сообщения групп.\n'
                         'Сейчас: ' + (', '.join(map(str, selected)) if selected else 'все группы'), buttons)

    async def event_callback(self, event, callback):
        if not await self.require_admin(event):
            return
        parts = callback.split(':')
        try:
            op = parts[1]
            if op not in {'kind', 'anyuser'}:
                self.storage.cancel(event.sender_id)
            if op == 'home':
                await self.event_home(event)
            elif op == 'list':
                await self.event_catalog(event, parts[2], int(parts[3]))
            elif op == 'view':
                self.storage.cancel(event.sender_id)
                await self.event_detail(event, parts[2], parts[3])
            elif op == 'new':
                cat = parts[2]
                if cat not in CATEGORIES:
                    raise ValueError('Неизвестный раздел.')
                self.storage.set_draft(event.sender_id, 'auto_name', json.dumps({'cat': cat}))
                await self.reply(event, 'Введите название (до 80 символов).', CANCEL)
            elif op == 'kind':
                draft = self.storage.draft(event.sender_id)
                if not draft or draft['stage'] != 'auto_kind':
                    raise ValueError('Выбор типа устарел. Начните создание заново.')
                payload = json.loads(draft['name'])
                allowed = CONDITIONS if payload['cat'] == 'c' else ACTIONS
                if parts[2] not in allowed or parts[2] == 'legacy_sticker':
                    raise ValueError('Неизвестный тип.')
                payload['kind'] = parts[2]
                await self.event_config_prompt(event, payload)
            elif op == 'bind':
                rule_id, block_id = long(parts[2]), long(parts[3])
                self.storage.toggle_link(rule_id, block_id)
                await self.event_detail(event, 'e', rule_id)
            elif op == 'pick':
                self.event_item('e', parts[3])
                if parts[2] not in {'c', 'a'}:
                    raise ValueError('Выберите условие или действие.')
                await self.event_catalog(event, parts[2], int(parts[4]), parts[3])
            elif op == 'toggle':
                cat, item_id = parts[2:4]
                row = self.event_item(cat, item_id)
                if cat == 'e':
                    if not row['enabled']:
                        linked = self.storage.linked_blocks(item_id)
                        if len(linked) != 2 or {b['category'] for b in linked} != {'condition', 'action'} or not all(b['enabled'] for b in linked):
                            raise ValueError('Выберите одно включённое условие и одно включённое действие.')
                        condition = next(b for b in linked if b['category'] == 'condition')
                        action = next(b for b in linked if b['category'] == 'action')
                        if condition['kind'] in {'join', 'leave'} and action['kind'] == 'reaction':
                            raise ValueError('Для входа/выхода выберите действие, которому не требуется сообщение: например текст или стикер.')
                    self.storage.change_event(item_id, 'enabled', 1 - row['enabled'])
                else:
                    self.storage.toggle_block(item_id)
                await self.event_detail(event, cat, item_id)
            elif op in {'edit', 'rename'}:
                cat, item_id = parts[2:4]
                row = self.event_item(cat, item_id)
                payload = {'cat': cat, 'id': item_id, 'name': row['name']}
                if cat != 'e':
                    payload.update(kind=row['kind'], data=json.loads(row['data']))
                if op == 'rename':
                    self.storage.set_draft(event.sender_id, 'auto_rename', json.dumps(payload))
                    await self.reply(event, 'Введите новое название (до 80 символов).', CANCEL)
                else:
                    await self.event_config_prompt(event, payload)
            elif op == 'setting':
                field, item_id = parts[2:4]
                self.event_item('e', item_id)
                prompts = {'chance': 'Введите вероятность 0–100%.', 'cooldown': 'Введите паузу между срабатываниями в одном чате в секундах: 0–86400.',
                           'chats': 'Введите отрицательные Telegram ID групп через запятую. Для всех групп используйте кнопку «Все группы».'}
                self.storage.set_draft(event.sender_id, 'auto_setting', json.dumps({'id': item_id, 'field': field}))
                await self.reply(event, prompts[field], CANCEL)
            elif op == 'chats':
                await self.event_chats(event, parts[2], int(parts[3]))
            elif op in {'chat', 'everywhere'}:
                item_id = long(parts[2]) if op == 'chat' else parts[2]
                row = self.event_item('e', item_id)
                chats = set(json.loads(row['chats']))
                if op == 'chat':
                    chat_id = int(parts[3])
                    if chat_id not in {c['id'] for c in self.storage.known_chats()}:
                        raise ValueError('Этот чат ещё неизвестен боту.')
                    chats.symmetric_difference_update({chat_id})
                else:
                    chats.clear()
                self.storage.change_event(item_id, 'chats', json.dumps(sorted(chats)))
                await self.event_chats(event, item_id)
            elif op in {'askdelete', 'delete'}:
                cat, item_id = parts[2:4]
                self.event_item(cat, item_id)
                if op == 'askdelete':
                    await self.reply(event, 'Удалить элемент? События, использующие удалённое условие или действие, будут выключены.',
                                     [[button('Да, удалить', f'ev:delete:{cat}:{item_id}')], [button('Нет', f'ev:view:{cat}:{item_id}')]])
                else:
                    self.storage.delete_automation(CATEGORIES[cat], item_id)
                    self.storage.cancel(event.sender_id)
                    await self.event_catalog(event, cat)
            elif op == 'anyuser':
                draft = self.storage.draft(event.sender_id)
                if not draft or draft['stage'] != 'auto_target':
                    raise ValueError('Кнопка устарела.')
                payload = json.loads(draft['name'])
                if payload['kind'] == 'user':
                    raise ValueError('Для этого условия требуется пользователь.')
                payload['data'].pop('user_id', None)
                await self.event_save_block(event, payload)
        except (ValueError, KeyError, IndexError) as exc:
            await self.reply(event, str(exc) if isinstance(exc, ValueError) else 'Кнопка устарела. Откройте раздел заново.', CANCEL)

    async def event_config_prompt(self, event, payload):
        kind = payload['kind']
        payload['data'] = {}
        if payload['cat'] == 'c' and kind in {'message', 'join', 'leave', 'user'}:
            await self.event_target_prompt(event, payload)
            return
        if kind in {'mention', 'all'}:
            await self.event_save_block(event, payload)
            return
        prompts = {'keywords': 'Введите слова или фразы через запятую либо по одному на строке (до 50). Совпадение — любое целое слово/фраза без учёта регистра.',
                   'rate': 'Сколько сообщений должно быть отправлено? Введите число 1–10000.',
                   'text': 'Введите сообщение (до 2000 символов).', 'reply': 'Введите текст ответа (до 2000 символов).',
                   'reaction': 'Введите emoji реакции, например 👍 или 🔥. Реакция должна быть разрешена в целевом чате.',
                   'mentions': 'Введите @username через пробел (до 200).',
                   'poll': 'Введите вопрос первой строкой, затем 2–10 вариантов, каждый с новой строки.',
                   'dice': 'Введите один emoji: 🎲 🎯 🏀 ⚽ 🎳 🎰.'}
        prompt = 'Отправьте медиа выбранного типа: ' + ACTIONS[kind] if kind in MEDIA else prompts[kind]
        self.storage.set_draft(event.sender_id, 'auto_config', json.dumps(payload))
        await self.reply(event, prompt + '\nИзменения применятся ко всем событиям, использующим этот элемент.', CANCEL)

    async def event_target_prompt(self, event, payload):
        self.storage.set_draft(event.sender_id, 'auto_target', json.dumps(payload))
        buttons = CANCEL if payload['kind'] == 'user' else [[button('Любой пользователь / весь чат', 'ev:anyuser')]] + CANCEL
        await self.reply(event, 'Введите @username пользователя.' +
                         (' Если не нужен конкретный пользователь, нажмите кнопку ниже.' if payload['kind'] != 'user' else ''), buttons)

    async def event_save_block(self, event, payload):
        item_id = self.storage.put_block(CATEGORIES[payload['cat']], payload['name'], payload['kind'], payload['data'], payload.get('id'))
        self.storage.cancel(event.sender_id)
        await self.event_detail(event, payload['cat'], item_id)

    async def event_input(self, event, draft):
        if not await self.require_admin(event):
            return
        payload = json.loads(draft['name'])
        text = event.raw_text.strip()
        stage = draft['stage']
        try:
            if stage in {'auto_name', 'auto_rename'}:
                if not text or len(text) > 80:
                    raise ValueError('Название: от 1 до 80 символов.')
                payload['name'] = text
                if stage == 'auto_rename':
                    row = self.event_item(payload['cat'], payload['id'])
                    if payload['cat'] == 'e':
                        self.storage.change_event(payload['id'], 'name', text)
                    else:
                        self.storage.put_block(row['category'], text, row['kind'], json.loads(row['data']), row['id'])
                    self.storage.cancel(event.sender_id)
                    await self.event_detail(event, payload['cat'], payload['id'])
                elif payload['cat'] == 'e':
                    item_id = self.storage.create_event(text)
                    self.storage.cancel(event.sender_id)
                    await self.event_detail(event, 'e', item_id)
                else:
                    self.storage.set_draft(event.sender_id, 'auto_kind', json.dumps(payload))
                    choices = CONDITIONS if payload['cat'] == 'c' else ACTIONS
                    await self.reply(event, 'Выберите тип:', [[button(label, f'ev:kind:{kind}')]
                                     for kind, label in choices.items() if kind != 'legacy_sticker'] + CANCEL)
                return
            if stage == 'auto_setting':
                field = payload['field']
                if field == 'chance':
                    value = probability(text)
                elif field == 'cooldown':
                    value = integer(text, 0, 86400)
                else:
                    parts = [p.strip() for p in text.split(',')]
                    if len(parts) > 50 or any(not re.fullmatch(r'-[1-9][0-9]{0,17}', p) for p in parts):
                        raise ValueError('Введите до 50 отрицательных ID групп через запятую.')
                    value = json.dumps(sorted(set(map(int, parts))))
                self.storage.change_event(payload['id'], field, value)
                self.storage.cancel(event.sender_id)
                await self.event_detail(event, 'e', payload['id'])
                return
            if stage == 'auto_target':
                if not re.fullmatch(r'@[A-Za-z][A-Za-z0-9_]{0,31}', text):
                    raise ValueError('Введите @username или выберите «Любой пользователь».')
                try:
                    user = await self.client.get_entity(text)
                except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
                    raise ValueError('Пользователь не найден.') from None
                if not isinstance(user, types.User) or user.bot or user.deleted:
                    raise ValueError('Выберите пользователя, не бота или канал.')
                payload['data']['user_id'] = user.id
                await self.event_save_block(event, payload)
                return
            if stage == 'auto_seconds':
                payload['data']['seconds'] = integer(text, 1, 86400)
                await self.event_target_prompt(event, payload)
                return
            if stage != 'auto_config':
                raise ValueError('Выберите тип с помощью кнопки.')
            kind = payload['kind']
            data = payload['data']
            if kind == 'rate':
                data['count'] = integer(text, 1, 10000)
                self.storage.set_draft(event.sender_id, 'auto_seconds', json.dumps(payload))
                await self.reply(event, 'За сколько секунд? Введите 1–86400 (например 60 — одна минута).', CANCEL)
                return
            if kind == 'keywords':
                words = list(dict.fromkeys(w.strip() for w in re.split(r'[,\n]', text) if w.strip()))
                if not 1 <= len(words) <= 50 or any(len(w) > 100 for w in words):
                    raise ValueError('Введите 1–50 слов/фраз, каждое до 100 символов.')
                data['words'] = words
                await self.event_target_prompt(event, payload)
                return
            if kind in {'text', 'reply'}:
                if not text or len(text) > 2000:
                    raise ValueError('Текст: 1–2000 символов.')
                data['text'] = text
            elif kind in MEDIA:
                message = event.message
                media = getattr(message, kind, None)
                if media is None:
                    raise ValueError('Отправьте медиа нужного типа: ' + ACTIONS[kind])
                data.update(media=base64.b64encode(bytes(message.media)).decode(),
                            source_chat=event.chat_id, source_message=event.id)
            elif kind == 'reaction':
                if text not in REACTIONS:
                    raise ValueError('Выберите обычную реакцию, например 👍, ❤, 🔥 или 🎉.')
                data['emoji'] = text
            elif kind == 'mentions':
                data['members'] = parse_members(text)
            elif kind == 'dice':
                if text not in {'🎲', '🎯', '🏀', '⚽', '🎳', '🎰'}:
                    raise ValueError('Допустимые emoji: 🎲 🎯 🏀 ⚽ 🎳 🎰')
                data['emoji'] = text
            elif kind == 'poll':
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if not 3 <= len(lines) <= 11 or len(lines[0]) > 200 or any(len(x) > 100 for x in lines[1:]) or len(set(lines[1:])) != len(lines[1:]):
                    raise ValueError('Вопрос до 200 символов и 2–10 разных вариантов до 100 символов, по одному на строке.')
                data.update(question=lines[0], answers=lines[1:])
            await self.event_save_block(event, payload)
        except (ValueError, KeyError) as exc:
            await self.reply(event, str(exc) if isinstance(exc, ValueError) else 'Настройка устарела. Начните заново.', CANCEL)

"""A rule is exactly one reusable condition paired with one reusable action."""
import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass

from telethon import errors, functions, types, utils
from telethon.extensions import BinaryReader

from .mentions import user_batches, username_batches
from .storage import normalize

LOG = logging.getLogger(__name__)
CONDITIONS = {'keywords': 'Ключевые слова', 'rate': 'Количество сообщений',
              'join': 'Вход в беседу', 'leave': 'Выход из беседы',
              'user': 'Сообщения пользователя', 'message': 'Любое сообщение'}
ACTIONS = {'reply': 'Ответ текстом', 'text': 'Сообщение в чат', 'sticker': 'Стикер',
           'photo': 'Фото', 'video': 'Видео', 'gif': 'GIF', 'document': 'Документ',
           'voice': 'Голосовое', 'audio': 'Аудио', 'reaction': 'Реакция emoji',
           'mention': 'Упомянуть участника события', 'mentions': 'Упомянуть список @username',
           'all': 'Упомянуть всех', 'poll': 'Опрос', 'dice': 'Игровой emoji',
           'legacy_sticker': 'Сохранённый стикер'}
MEDIA = {'sticker', 'photo', 'video', 'gif', 'document', 'voice', 'audio'}
REACTIONS = ['👍', '👎', '❤', '🔥', '🥰', '👏', '😁', '🤔', '🤯', '😱', '🤬', '😢', '🎉', '🤩', '🤮', '💩', '🙏', '👌', '🕊', '🤡', '🥱', '🥴', '😍', '🐳', '❤‍🔥', '🌚', '🌭', '💯', '🤣', '⚡', '🍌', '🏆', '💔', '🤨', '😐', '🍓', '🍾', '💋', '🖕', '😈', '😴', '😭', '🤓', '👻', '👨‍💻', '👀', '🎃', '🙈', '😇', '😨', '🤝', '✍', '🤗', '🫡', '🎅', '🎄', '☃', '💅', '🤪', '🗿', '🆒', '💘', '🙉', '🦄', '😘', '💊', '🙊', '😎', '👾', '🤷‍♂', '🤷', '🤷‍♀', '😡']


@dataclass
class Trigger:
    kind: str
    chat_id: int
    user: object
    message_id: int | None = None
    text: str = ''
    peer: object = None
    now: float | None = None


def matches(store, block, trigger, now):
    if not block['enabled']:
        return False
    data = json.loads(block['data'])
    kind = block['kind']
    target = data.get('user_id')
    if target and target != trigger.user.id:
        return False
    if kind in {'join', 'leave'}:
        return trigger.kind == kind
    if trigger.kind != 'message':
        return False
    if kind in {'user', 'message'}:
        return True
    if kind == 'keywords':
        text = normalize(trigger.text)
        return any(re.search(r'(?<!\w)' + re.escape(normalize(word)) + r'(?!\w)', text)
                   for word in data['words'])
    if kind == 'rate':
        return store.count_messages(trigger.chat_id, target, now - data['seconds']) >= data['count']
    return False


class Automation:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.storage

    async def process(self, trigger):
        if not isinstance(trigger.user, types.User) or trigger.user.bot or trigger.user.deleted or trigger.chat_id >= 0:
            return
        now = trigger.now if trigger.now is not None else time.time()
        key = f'm:{trigger.message_id}' if trigger.kind == 'message' else f'{trigger.kind}:{trigger.user.id}'
        # Reserve runs before network I/O; concurrent updates cannot duplicate a rule.
        pending = []
        async with self.bot.lock(('automation', trigger.chat_id)):
            if not self.store.observe(trigger.chat_id, key, trigger.user.id, trigger.message_id, now, trigger.kind):
                return
            for rule in self.store.event_rules():
                chats = json.loads(rule['chats'])
                if not rule['enabled'] or (chats and trigger.chat_id not in chats):
                    continue
                blocks = self.store.linked_blocks(rule['id'])
                conditions = [b for b in blocks if b['category'] == 'condition']
                actions = [b for b in blocks if b['category'] == 'action']
                if len(conditions) != 1 or len(actions) != 1 or not actions[0]['enabled']:
                    continue
                if not matches(self.store, conditions[0], trigger, now):
                    continue
                if now - self.store.last_run(rule['id'], trigger.chat_id) < rule['cooldown']:
                    continue
                if rule['chance'] <= 0 or random.random() * 100 >= rule['chance']:
                    continue
                self.store.record_run(rule['id'], trigger.chat_id, now)
                pending.append((rule['id'], actions[0]))
        for event_id, action in pending:
            # Recheck after awaits: an administrator may have disabled/deleted a rule.
            current = self.store.event_rule(event_id)
            block = self.store.block(action['id'])
            if not current or not current['enabled'] or not block or not block['enabled']:
                continue
            current_chats = json.loads(current['chats'])
            if current['chance'] <= 0 or (current_chats and trigger.chat_id not in current_chats):
                continue
            linked = self.store.linked_blocks(event_id)
            conditions = [b for b in linked if b['category'] == 'condition']
            if len(conditions) != 1 or block['id'] not in {b['id'] for b in linked} or not matches(self.store, conditions[0], trigger, now):
                continue
            if time.monotonic() < self.bot.blocked_until:
                break
            try:
                await self.perform(block, trigger)
            except errors.FloodWaitError as exc:
                self.bot.blocked_until = time.monotonic() + exc.seconds
                LOG.warning('Event %s FloodWait: %s seconds', event_id, exc.seconds)
                break
            except Exception as exc:
                LOG.warning('Event %s action %s failed in chat %s: %s', event_id, action['id'], trigger.chat_id, type(exc).__name__)

    async def perform(self, action, trigger):
        client = self.bot.client
        data = json.loads(action['data'])
        kind = action['kind']
        chat = trigger.peer if trigger.peer is not None else trigger.chat_id
        # Leave updates may not have a message to reply to.
        reply_to = trigger.message_id
        if kind in {'text', 'reply'}:
            await client.send_message(chat, data['text'], reply_to=reply_to if kind == 'reply' else None,
                                      parse_mode=None, link_preview=False)
        elif kind == 'legacy_sticker':
            row = self.store.sticker_rule(data['rule_id'])
            if row:
                await self.bot.send_saved_sticker(chat, row, reply_to=reply_to)
        elif kind in MEDIA:
            await self.send_media(action, chat, reply_to)
        elif kind == 'reaction':
            if reply_to is not None:
                await client(functions.messages.SendReactionRequest(chat, reply_to, reaction=[types.ReactionEmoji(data['emoji'])]))
        elif kind == 'mentions':
            for batch in username_batches(data['members']):
                await client.send_message(chat, batch, reply_to=reply_to, parse_mode=None)
        elif kind in {'mention', 'all'}:
            people = [trigger.user] if kind == 'mention' else await client.get_participants(chat, limit=None)
            if kind == 'all' and people.total is not None and len(people) < people.total:
                await client.send_message(chat, 'Telegram вернул неполный список. Упомяну доступных участников.', reply_to=reply_to)
            for text, entities in user_batches(people):
                await client.send_message(chat, text, formatting_entities=entities, reply_to=reply_to, parse_mode=None)
        elif kind == 'dice':
            await client.send_file(chat, types.InputMediaDice(data['emoji']), reply_to=reply_to)
        elif kind == 'poll':
            poll = types.Poll(id=0, question=types.TextWithEntities(data['question'], []),
                              answers=[types.PollAnswer(types.TextWithEntities(answer, []), bytes([i]))
                                       for i, answer in enumerate(data['answers'])], hash=0)
            await client.send_file(chat, types.InputMediaPoll(poll), reply_to=reply_to)
        else:
            raise ValueError('Unknown action kind')

    async def send_media(self, action, chat, reply_to):
        data = json.loads(action['data'])
        with BinaryReader(base64.b64decode(data['media'])) as reader:
            media = reader.tgread_object()
        try:
            await self.bot.client.send_file(chat, media, reply_to=reply_to)
        except (errors.FileReferenceExpiredError, errors.FileReferenceInvalidError):
            original = await self.bot.client.get_messages(data['source_chat'], ids=data['source_message'])
            fresh = original.media if original else None
            if fresh is None:
                raise ValueError('Исходное медиа удалено. Замените его через меню действий.')
            # Persist only if no administrator changed this action while fetching.
            current = self.store.block(action['id'])
            if current and current['data'] == action['data']:
                data['media'] = base64.b64encode(bytes(fresh)).decode()
                self.store.put_block('action', current['name'], current['kind'], data, action['id'])
            await self.bot.client.send_file(chat, fresh, reply_to=reply_to)

    async def membership(self, event):
        if not event.is_group or time.monotonic() < self.bot.blocked_until:
            return
        kind = 'join' if event.user_joined or event.user_added else 'leave' if event.user_left or event.user_kicked else None
        if not kind:
            return
        try:
            message = event.action_message
            for user in await event.get_users():
                await self.process(Trigger(kind, event.chat_id, user, getattr(message, 'id', None),
                                           peer=await event.get_input_chat()))
        except Exception as exc:
            LOG.warning('Membership update failed: %s', type(exc).__name__)

    async def participant_update(self, update):
        # Admin-only updates cover changes for which no service message is emitted.
        def present(participant):
            return participant is not None and not isinstance(participant, types.ChannelParticipantLeft) and not getattr(participant, 'left', False)
        before, after = present(update.prev_participant), present(update.new_participant)
        if before == after or time.monotonic() < self.bot.blocked_until:
            return
        try:
            chat_id = utils.get_peer_id(types.PeerChannel(update.channel_id))
            chat = await self.bot.client.get_entity(types.PeerChannel(update.channel_id))
            if not getattr(chat, 'megagroup', False):
                return
            user = await self.bot.client.get_entity(update.user_id)
            await self.process(Trigger('join' if after else 'leave', chat_id, user))
        except Exception as exc:
            LOG.warning('Participant update unavailable: %s', type(exc).__name__)

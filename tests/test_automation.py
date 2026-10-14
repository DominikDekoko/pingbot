import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors, types, TelegramClient, utils

from pingbot.automation import Trigger
from pingbot.bot import Bot
from pingbot.event_menus import short
from pingbot.storage import Storage


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Storage(':memory:')
        self.client = AsyncMock()
        self.config = SimpleNamespace(admin_ids=frozenset({1}), cooldown_seconds=0, all_allow_everyone=True)
        self.bot = Bot(self.client, self.db, self.config, 'pingbot')
        self.user = types.User(id=10, first_name='Друг', access_hash=42)

    async def asyncTearDown(self):
        self.db.close()

    def pair(self, kind='keywords', data=None, action='reply', action_data=None, condition_id=None, action_id=None):
        condition_id = condition_id or self.db.put_block('condition', 'Условие', kind, data if data is not None else {'words': ['злой']})
        action_id = action_id or self.db.put_block('action', 'Действие', action, action_data if action_data is not None else {'text': 'Привет'})
        rule = self.db.create_event('Пара')
        self.db.toggle_link(rule, condition_id)
        self.db.toggle_link(rule, action_id)
        self.db.change_event(rule, 'cooldown', 0)
        self.db.change_event(rule, 'enabled', 1)
        return rule, condition_id, action_id

    async def trigger(self, message=1, now=100, text='злой', user=None, chat=-100, kind='message'):
        await self.bot.automation.process(Trigger(kind, chat, user or self.user, message, text, peer=chat, now=now))

    async def test_keywords_whole_words_case_and_list(self):
        self.pair(data={'words': ['злой', 'иду домой']})
        for i, text in enumerate(['ЗЛОЙ!', 'он незлой', 'я ИДУ   домой', 'иду домойка']):
            await self.trigger(message=i, text=text)
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_all_four_independent_pairs_and_shared_disable(self):
        first = self.pair()
        second = self.pair()
        self.pair(condition_id=first[1], action_id=second[2])
        self.pair(condition_id=second[1], action_id=first[2])
        await self.trigger()
        self.assertEqual(self.client.send_message.await_count, 4)
        self.db.toggle_block(first[1])
        await self.trigger(message=2)
        self.assertEqual(self.client.send_message.await_count, 6)
        self.db.toggle_block(second[2])
        await self.trigger(message=3)
        self.assertEqual(self.client.send_message.await_count, 7)

    async def test_rate_whole_chat_and_user_filter(self):
        self.pair('rate', {'count': 3, 'seconds': 10})
        self.pair('rate', {'count': 2, 'seconds': 10, 'user_id': 10})
        await self.trigger(1, 100)
        await self.trigger(2, 101, user=types.User(id=11))
        self.client.send_message.assert_not_awaited()
        await self.trigger(3, 102)
        self.assertEqual(self.client.send_message.await_count, 2)
        await self.trigger(4, 113)
        self.assertEqual(self.client.send_message.await_count, 2)
        await self.trigger(5, 114, chat=-200)
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_dedup_concurrency_cooldown_and_probability(self):
        rule, _, _ = self.pair()
        self.db.change_event(rule, 'cooldown', 10)
        await asyncio.gather(self.trigger(), self.trigger())
        self.client.send_message.assert_awaited_once()
        await self.trigger(2, 109)
        self.client.send_message.assert_awaited_once()
        await self.trigger(3, 110)
        self.assertEqual(self.client.send_message.await_count, 2)
        self.db.change_event(rule, 'chance', 0)
        await self.trigger(4, 200)
        self.assertEqual(self.client.send_message.await_count, 2)
        self.db.change_event(rule, 'chance', 25)
        with patch('pingbot.automation.random.random', return_value=0.25):
            await self.trigger(5, 201)
        with patch('pingbot.automation.random.random', return_value=0.249):
            await self.trigger(6, 202)
        self.assertEqual(self.client.send_message.await_count, 3)

    async def test_selected_chat_and_disabled_event(self):
        rule, _, _ = self.pair()
        self.db.change_event(rule, 'chats', '[-200]')
        await self.trigger()
        self.client.send_message.assert_not_awaited()
        await self.trigger(chat=-200)
        self.client.send_message.assert_awaited_once()
        self.db.change_event(rule, 'enabled', 0)
        await self.trigger(message=2, chat=-200)
        self.client.send_message.assert_awaited_once()

    async def test_single_pair_replacement_and_delete_disables_dependents(self):
        rule, condition, action = self.pair()
        replacement = self.db.put_block('condition', 'Вход', 'join', {})
        self.db.toggle_link(rule, replacement)
        self.assertEqual({b['id'] for b in self.db.linked_blocks(rule)}, {replacement, action})
        self.assertFalse(self.db.event_rule(rule)['enabled'])
        self.db.change_event(rule, 'enabled', 1)
        self.db.delete_automation('condition', replacement)
        self.assertFalse(self.db.event_rule(rule)['enabled'])
        self.assertIsNotNone(self.db.block(condition))

    async def test_join_leave_and_cross_source_dedup(self):
        self.pair('join', {})
        self.pair('leave', {})
        await self.trigger(kind='join', message=None)
        await self.trigger(kind='join', message=50, now=101)
        self.client.send_message.assert_awaited_once()
        self.assertIsNone(self.client.send_message.call_args.kwargs['reply_to'])
        await self.trigger(kind='leave', message=None, now=102)
        self.assertEqual(self.client.send_message.await_count, 2)
        await self.trigger(kind='message', message=51, now=103)
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_bot_and_private_messages_ignored(self):
        self.pair('message', {})
        await self.trigger(user=types.User(id=11, bot=True))
        await self.trigger(chat=11)
        self.client.send_message.assert_not_awaited()

    async def test_action_failure_does_not_stop_other_events(self):
        self.pair()
        self.pair()
        self.client.send_message.side_effect = [errors.ChatWriteForbiddenError(request=None), None]
        await self.trigger()
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_floodwait_stops_execution(self):
        self.pair()
        self.pair()
        self.client.send_message.side_effect = errors.FloodWaitError(request=None, capture=60)
        await self.trigger()
        self.client.send_message.assert_awaited_once()
        self.assertGreater(self.bot.blocked_until, 0)

    async def test_reaction_poll_dice_use_telegram_types(self):
        self.pair(action='reaction', action_data={'emoji': '👍'})
        self.pair(action='poll', action_data={'question': 'Играем?', 'answers': ['Да', 'Нет']})
        self.pair(action='dice', action_data={'emoji': '🎲'})
        await self.trigger()
        request = self.client.call_args.args[0]
        self.assertEqual(request.msg_id, 1)
        self.client.get_input_entity.return_value = types.InputPeerChat(100)
        await request.resolve(self.client, utils)
        self.assertTrue(bytes(request))
        media = [c.args[1] for c in self.client.send_file.call_args_list]
        self.assertIsInstance(media[0], types.InputMediaPoll)
        self.assertIsInstance(media[1], types.InputMediaDice)
        for item in media:
            self.assertTrue(bytes(item))

    async def test_native_media_refresh_and_preview_metadata(self):
        original = types.MessageMediaDocument(document=types.DocumentEmpty(id=42))
        data = {'media': base64.b64encode(bytes(original)).decode(), 'source_chat': 1, 'source_message': 99}
        _, _, action = self.pair(action='document', action_data=data)
        fresh = types.MessageMediaDocument(document=types.DocumentEmpty(id=43))
        self.client.get_messages.return_value = SimpleNamespace(media=fresh)
        self.client.send_file.side_effect = [errors.FileReferenceExpiredError(request=None), None]
        await self.trigger()
        self.client.get_messages.assert_awaited_once_with(1, ids=99)
        self.assertEqual(self.client.send_file.call_args.args[1].document.id, 43)
        self.assertEqual(json.loads(self.db.block(action)['data'])['media'], base64.b64encode(bytes(fresh)).decode())

    async def test_counter_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Storage(Path(directory) / 'bot.db')
            db.observe(-100, 'm:1', 10, 1, 100, 'message')
            db.close()
            db = Storage(Path(directory) / 'bot.db')
            self.assertEqual(db.count_messages(-100, None, 99), 1)
            self.assertFalse(db.observe(-100, 'm:1', 10, 1, 101, 'message'))
            db.close()

    def event(self, text='', owner=1):
        return SimpleNamespace(raw_text=text, sender_id=owner, chat_id=owner, id=50,
                               is_private=True, is_group=False, reply=AsyncMock(),
                               get_sender=AsyncMock(return_value=types.User(id=owner)))

    async def click(self, data, owner=1):
        event = self.event(owner=owner)
        event.data = data.encode()
        event.answer = AsyncMock()
        await self.bot.handle(event)
        return event

    async def input(self, text):
        event = self.event(text)
        await self.bot.handle(event)
        return event

    async def test_button_wizard_condition_action_pair(self):
        await self.click('ev:new:c')
        await self.input('Злые слова')
        await self.click('ev:kind:keywords')
        await self.input('злой, сердитый')
        await self.click('ev:anyuser')
        condition = self.db.blocks('condition')[0]
        await self.click('ev:new:a')
        await self.input('Добрый ответ')
        await self.click('ev:kind:reply')
        await self.input('Не сердись!')
        action = self.db.blocks('action')[0]
        await self.click('ev:new:e')
        await self.input('Ответ на злой')
        rule = self.db.event_rules()[0]
        self.assertFalse(rule['enabled'])
        await self.click(f"ev:bind:{short(rule['id'])}:{short(condition['id'])}")
        await self.click(f"ev:bind:{short(rule['id'])}:{short(action['id'])}")
        await self.click(f"ev:toggle:e:{rule['id']}")
        self.assertTrue(self.db.event_rule(rule['id'])['enabled'])
        await self.trigger()
        self.assertEqual(self.client.send_message.call_args.args[1], 'Не сердись!')

    async def test_rate_wizard_default_whole_chat(self):
        await self.click('ev:new:c')
        await self.input('Много сообщений')
        await self.click('ev:kind:rate')
        await self.input('3')
        await self.input('60')
        await self.click('ev:anyuser')
        data = json.loads(self.db.blocks('condition')[0]['data'])
        self.assertEqual(data, {'count': 3, 'seconds': 60})

    async def test_permissions_revocation_and_incomplete_rule(self):
        await self.click('ev:new:e', owner=2)
        self.assertEqual(self.db.event_rules(), [])
        await self.click('ev:new:c')
        self.config.admin_ids = frozenset()
        await self.input('Denied')
        self.assertEqual(self.db.blocks('condition'), [])
        self.config.admin_ids = frozenset({1})
        rule = self.db.create_event('Empty')
        await self.click(f'ev:toggle:e:{rule}')
        self.assertFalse(self.db.event_rule(rule)['enabled'])

    async def test_selector_callback_payload_and_pagination(self):
        rule, _, _ = self.pair()
        for i in range(10):
            self.db.put_block('condition', f'Слово {i}', 'keywords', {'words': ['тест']})
        event = await self.click(f'ev:pick:c:{rule}:1')
        markup = TelegramClient.build_reply_markup(event.reply.call_args.kwargs['buttons'])
        for row in markup.rows:
            for b in row.buttons:
                self.assertLessEqual(len(b.type.data), 64)
        self.assertIn('2/2', event.reply.call_args.args[0])

    async def test_membership_adapter_targets_joined_user_not_actor(self):
        self.pair('join', {'user_id': 10})
        event = SimpleNamespace(is_group=True, user_joined=True, user_added=False, user_left=False,
                                user_kicked=False, action_message=SimpleNamespace(id=10), chat_id=-100,
                                get_users=AsyncMock(return_value=[self.user]), get_input_chat=AsyncMock(return_value=-100))
        await self.bot.automation.membership(event)
        self.client.send_message.assert_awaited_once()

    async def test_raw_participant_update_and_restriction_not_a_leave(self):
        self.pair('leave', {})
        self.client.get_entity.side_effect = [SimpleNamespace(megagroup=True), self.user]
        update = SimpleNamespace(channel_id=123, user_id=10,
                                 prev_participant=types.ChannelParticipant(user_id=10, date=None),
                                 new_participant=types.ChannelParticipantLeft(peer=types.PeerUser(10)))
        await self.bot.automation.participant_update(update)
        self.client.send_message.assert_awaited_once()
        # Restriction while remaining in the group is not an exit.
        update.new_participant = SimpleNamespace(left=False)
        await self.bot.automation.participant_update(update)
        self.client.send_message.assert_awaited_once()

    async def test_join_reaction_pair_cannot_be_enabled(self):
        rule, _, _ = self.pair('join', {}, action='reaction', action_data={'emoji': '👍'})
        self.db.change_event(rule, 'enabled', 0)
        await self.click(f'ev:toggle:e:{rule}')
        self.assertFalse(self.db.event_rule(rule)['enabled'])

    async def test_chats_and_chance_configuration(self):
        rule, _, _ = self.pair()
        self.db.remember_chat(-100, 'Тестовая группа')
        await self.click(f'ev:chat:{short(rule)}:-100')
        self.assertEqual(json.loads(self.db.event_rule(rule)['chats']), [-100])
        await self.click(f'ev:setting:chance:{rule}')
        await self.input('12,5%')
        self.assertEqual(self.db.event_rule(rule)['chance'], 12.5)
        await self.click(f'ev:setting:cooldown:{rule}')
        await self.input('NaN')
        self.assertEqual(self.db.event_rule(rule)['cooldown'], 0)
        await self.click(f'ev:everywhere:{rule}')
        self.assertIsNone(self.db.draft(1))
        self.assertEqual(json.loads(self.db.event_rule(rule)['chats']), [])


class MigrationTests(unittest.TestCase):
    def test_existing_sticker_rules_migrate_once_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            db = Storage(path)
            db.register(1)
            db.save(1, 'дота', ['@alice'])
            rule = db.save_sticker_rule(1, 10, 'alice', 17, b'document', b'content')
            tables = [r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'automation_%'")]
            with db.db:
                for table in tables:
                    db.db.execute(f'DROP TABLE {table}')
                db.db.execute('PRAGMA user_version=2')
            db.close()
            db = Storage(path)
            self.assertEqual(db.event_rule(rule)['chance'], 17)
            self.assertEqual(len(db.linked_blocks(rule)), 2)
            self.assertEqual(db.sticker_rule(rule)['content'], b'content')
            self.assertIsNotNone(db.get(1, 'дота'))
            db.change_event(rule, 'enabled', 0)
            db.close()
            db = Storage(path)
            self.assertEqual(len(db.event_rules()), 1)
            self.assertFalse(db.event_rule(rule)['enabled'])
            db.close()

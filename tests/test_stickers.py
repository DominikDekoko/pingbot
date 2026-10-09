import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import TelegramClient, errors
from telethon.tl.types import Document, DocumentAttributeSticker, InputStickerSetEmpty, User

from pingbot.bot import Bot
from pingbot.config import parse_admin_ids
from pingbot.stickers import probability
from pingbot.storage import Storage


def sticker(mime='image/webp'):
    return Document(id=42, access_hash=123, file_reference=b'old-reference',
                    date=datetime.now(timezone.utc), mime_type=mime, size=4, dc_id=2,
                    attributes=[DocumentAttributeSticker(alt='🙂', stickerset=InputStickerSetEmpty())])


class ConfigTests(unittest.TestCase):
    def test_admin_ids(self):
        self.assertEqual(parse_admin_ids(' 123, 456,123 '), frozenset({123, 456}))
        self.assertEqual(parse_admin_ids(' '), frozenset())
        for value in ('0', '-1', '@user', '12,,13', '1,', '1.5', str(2**63)):
            with self.assertRaises(ValueError):
                parse_admin_ids(value)

    def test_probability(self):
        for value, expected in [('0', 0), ('100%', 100), ('12,5', 12.5)]:
            self.assertEqual(probability(value), expected)
        for value in ('-1', '101', 'NaN', 'inf', 'abc', ''):
            with self.assertRaises(ValueError):
                probability(value)

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bot.sqlite3'
            db = Storage(path)
            rule = db.save_sticker_rule(1, 10, 'target', 25, bytes(sticker()), b'data')
            db.close()
            db = Storage(path)
            self.assertEqual(db.sticker_for_user(10)['chance'], 25)
            self.assertEqual(db.sticker_rule(rule)['content'], b'data')
            db.close()


class StickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Storage(':memory:')
        self.config = SimpleNamespace(admin_ids=frozenset({1, 2}), cooldown_seconds=0, all_allow_everyone=True)
        self.client = SimpleNamespace(get_entity=AsyncMock(return_value=User(id=10, username='target')),
                                      download_media=AsyncMock(return_value=b'data'), send_file=AsyncMock())
        self.bot = Bot(self.client, self.db, self.config, 'pingbot')

    async def asyncTearDown(self):
        self.db.close()

    def event(self, text='', owner=1, private=True, document=None):
        return SimpleNamespace(raw_text=text, sender_id=owner, chat_id=owner if private else -100,
                               id=50, is_private=private, is_group=not private, reply=AsyncMock(),
                               message=SimpleNamespace(sticker=document, action=None),
                               get_sender=AsyncMock(return_value=User(id=owner)),
                               get_input_chat=AsyncMock(return_value=owner if private else -100))

    async def click(self, data, owner=1, private=True):
        event = self.event(owner=owner, private=private)
        event.data = data.encode()
        event.answer = AsyncMock()
        await self.bot.handle(event)
        return event

    def create_rule(self, chance=100):
        return self.db.save_sticker_rule(1, 10, 'target', chance, bytes(sticker()), b'data')

    async def test_admin_keyboard_and_access_controls(self):
        for owner in (1, 3):
            event = self.event('/start', owner=owner)
            await self.bot.handle(event)
            markup = TelegramClient.build_reply_markup(event.reply.call_args.kwargs['buttons'])
            labels = [b.text for row in markup.rows for b in row.buttons]
            self.assertEqual('Стикер-ответы' in labels, owner == 1)
        rule = self.create_rule()
        for action in ('add', f'view:{rule}', f'edit:{rule}', f'chance:{rule}', f'delete:{rule}'):
            await self.click('sticker:' + action, owner=3)
            self.assertIsNone(self.db.draft(3))
            self.assertIsNotNone(self.db.sticker_rule(rule))
        await self.click(f'sticker:delete:{rule}', owner=1, private=False)
        self.assertIsNotNone(self.db.sticker_rule(rule))

    async def test_full_wizard_and_shared_edit_delete(self):
        await self.click('sticker:add')
        await self.bot.handle(self.event('@target'))
        self.assertEqual(self.db.draft(1)['stage'], 'admin_sticker')
        await self.bot.handle(self.event('not a sticker'))
        self.client.download_media.assert_not_awaited()
        await self.bot.handle(self.event(document=sticker()))
        self.assertIsNone(self.db.sticker_for_user(10))
        await self.bot.handle(self.event('25%'))
        rule = self.db.sticker_for_user(10)
        self.assertEqual(rule['chance'], 25)
        self.assertIsNone(self.db.draft(1))
        await self.click(f"sticker:chance:{rule['id']}", owner=2)
        await self.bot.handle(self.event('0', owner=2))
        self.assertEqual(self.db.sticker_for_user(10)['chance'], 0)
        await self.click(f"sticker:askdelete:{rule['id']}", owner=2)
        self.assertIsNotNone(self.db.sticker_rule(rule['id']))
        await self.click(f"sticker:delete:{rule['id']}", owner=2)
        self.assertIsNone(self.db.sticker_for_user(10))

    async def test_revoked_admin_cannot_finish_draft(self):
        await self.click('sticker:add')
        self.config.admin_ids = frozenset({2})
        await self.bot.handle(self.event('@target'))
        self.client.get_entity.assert_not_awaited()
        self.assertIsNone(self.db.draft(1))

    async def test_media_message_probability_and_exact_reply(self):
        rule = self.create_rule(25)
        with patch('pingbot.stickers.random.random', return_value=0.249):
            await self.bot.handle(self.event(owner=10, private=False))
        call = self.client.send_file.call_args
        self.assertEqual(call.args[0], -100)
        self.assertEqual(call.args[1].getvalue(), b'data')
        self.assertEqual(call.kwargs['reply_to'], 50)
        self.assertEqual(call.kwargs['mime_type'], 'image/webp')
        self.assertIsInstance(call.kwargs['attributes'][0], DocumentAttributeSticker)
        self.client.send_file.reset_mock()
        with patch('pingbot.stickers.random.random', return_value=0.25):
            await self.bot.handle(self.event('test', owner=10, private=False))
        self.client.send_file.assert_not_awaited()
        self.db.update_sticker_rule(1, rule, chance=0)
        with patch('pingbot.stickers.random.random', return_value=0):
            await self.bot.handle(self.event('test', owner=10, private=False))
        self.client.send_file.assert_not_awaited()

    async def test_no_replies_to_other_users_private_or_service_events(self):
        self.create_rule()
        await self.bot.handle(self.event(owner=11, private=False))
        await self.bot.handle(self.event(owner=10))
        service = self.event(owner=10, private=False)
        service.message.action = object()
        await self.bot.handle(service)
        self.client.send_file.assert_not_awaited()

    async def test_sticker_failure_does_not_break_phrase_trigger(self):
        self.create_rule()
        self.db.register(10)
        self.db.save(10, 'дота 2', ['@alice'])
        self.client.send_file.side_effect = errors.ChatWriteForbiddenError(request=None)
        event = self.event('я иду домой дота 2', owner=10, private=False)
        await self.bot.handle(event)
        self.assertEqual(event.reply.call_args.args[0], '@alice')

    async def test_edit_sticker_and_expired_reference_independent_upload(self):
        rule = self.create_rule()
        await self.click(f'sticker:edit:{rule}')
        await self.bot.handle(self.event(document=sticker('video/webm')))
        await self.click(f'sticker:preview:{rule}')
        self.assertEqual(self.client.send_file.call_args.kwargs['mime_type'], 'video/webm')
        self.assertEqual(self.client.send_file.call_args.args[1].name, 'sticker.webm')

    async def test_cancel_keeps_existing_rule(self):
        rule = self.create_rule()
        await self.click(f'sticker:chance:{rule}')
        await self.bot.handle(self.event('NaN'))
        self.assertEqual(self.db.sticker_rule(rule)['chance'], 100)
        await self.click('home')
        self.assertIsNone(self.db.draft(1))

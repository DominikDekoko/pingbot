import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors
from telethon.helpers import TotalList
from telethon.network.connection.tcpmtproxy import TcpMTProxy
from telethon.tl.types import User

from pingbot.bot import Bot
from pingbot.config import boolean
from pingbot.mentions import ALL_PATTERN, parse_members, user_batches
from pingbot.storage import Storage


class StorageTests(unittest.TestCase):
    def test_owner_isolation_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.db"
            db = Storage(path)
            db.register(1)
            db.register(2)
            db.save(1, "Дота 2", ["@alice"])
            db.save(2, "дота 2", ["@bobby"])
            db.save_phrase(1, "играем", db.phrases(1)[0]['id'])
            db.set_draft(1, "members", "новый")
            db.close()
            db = Storage(path)
            self.assertEqual(db.phrases(1)[0]['text'], "играем")
            self.assertEqual(db.phrases(2)[0]['text'], "я иду домой")
            self.assertEqual(db.draft(1)["name"], "новый")
            self.assertEqual(json.loads(db.get(1, " Дота   2 ")["members"]), ["@alice"])
            db.delete(1, "дота 2")
            self.assertIsNotNone(db.get(2, "дота 2"))
            db.close()

    def test_migrate_legacy_database_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'old.db'
            with sqlite3.connect(path) as old:
                old.executescript("""
                    CREATE TABLE users (id INTEGER PRIMARY KEY, phrase TEXT NOT NULL);
                    INSERT INTO users VALUES (1, 'иду на работу');
                    CREATE TABLE lists (owner INTEGER NOT NULL, name TEXT NOT NULL,
                        name_key TEXT NOT NULL, members TEXT NOT NULL, PRIMARY KEY(owner, name_key));
                    INSERT INTO lists VALUES (1, 'дота 2', 'дота 2', '["@alice"]');
                """)
            db = Storage(path)
            self.assertEqual(db.phrases(1)[0]['text'], 'иду на работу')
            original_id = db.get(1, 'дота 2')['id']
            db.delete_item(1, 'phrase', db.phrases(1)[0]['id'])
            db.close()
            db = Storage(path)
            db.register(1)
            self.assertEqual(db.phrases(1), [])
            self.assertEqual(db.get(1, 'дота 2')['id'], original_id)
            db.close()

    def test_validation(self):
        self.assertEqual(parse_members("@Alice @alice @bobby"), ["@alice", "@bobby"])
        for text in ("", "alice", "@alice, @bobby", "https://t.me/alice"):
            with self.assertRaises(ValueError):
                parse_members(text)
        self.assertTrue(boolean("True"))
        self.assertFalse(boolean("False"))
        with self.assertRaises(ValueError):
            boolean("yes")

    def test_all_boundaries(self):
        for text in ("@all", "привет @ALL!", "(@all)"):
            self.assertTrue(ALL_PATTERN.search(text))
        for text in ("@allison", "x@all", "@@all", "https://t.me/@all"):
            self.assertFalse(ALL_PATTERN.search(text))

    def test_mentions_utf16_batches_and_no_username(self):
        users = [User(id=i, access_hash=i, first_name="😀 Друг") for i in range(1, 43)]
        users += [users[0], User(id=100, bot=True), User(id=101, deleted=True)]
        batches = list(user_batches(users))
        self.assertEqual([len(e) for _, e in batches], [20, 20, 2])
        for text, entities in batches:
            encoded = text.encode("utf-16-le")
            for entity in entities:
                self.assertEqual(encoded[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le"), "😀 Друг")

    def test_proxy_secret_prefix(self):
        for start in ("dd", "ee", "ab"):
            secret = start + "12" * 15
            self.assertEqual(TcpMTProxy.normalize_secret("dd" + secret), bytes.fromhex(secret))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Storage(":memory:")
        self.config = SimpleNamespace(all_allow_everyone=True, cooldown_seconds=0)
        self.client = SimpleNamespace(get_permissions=AsyncMock(), get_participants=AsyncMock(), send_message=AsyncMock())
        self.bot = Bot(self.client, self.db, self.config, "pingbot")

    async def asyncTearDown(self):
        self.db.close()

    def event(self, text, owner=1, private=False):
        return SimpleNamespace(raw_text=text, sender_id=owner, chat_id=owner if private else -123,
                               id=50, is_private=private, is_group=not private, reply=AsyncMock(),
                               get_sender=AsyncMock(return_value=User(id=owner, first_name="User")),
                               get_input_chat=AsyncMock(return_value=-123))

    async def test_private_wizard_invalid_input_edit_and_cancel(self):
        await self.bot.handle(self.event('/start', private=True))
        await self.click('list:add')
        for text in ("дота 2", "@alice @bobby @ALICE"):
            await self.bot.handle(self.event(text, private=True))
        self.assertEqual(json.loads(self.db.get(1, "дота 2")["members"]), ["@alice", "@bobby"])
        await self.click(f"list:edit:{self.db.get(1, 'дота 2')['id']}")
        await self.bot.handle(self.event("bad input", private=True))
        self.assertIsNotNone(self.db.draft(1))
        await self.click('home')
        self.assertIsNone(self.db.draft(1))
        self.assertEqual(len(json.loads(self.db.get(1, "дота 2")["members"])), 2)

    async def click(self, data, owner=1, private=True):
        event = self.event('', owner, private)
        event.data = data.encode()
        event.answer = AsyncMock()
        await self.bot.handle(event)
        event.answer.assert_awaited_once()
        return event

    async def test_all_phrase_list_combinations(self):
        self.db.register(1)
        self.db.save_phrase(1, 'я иду на работу')
        self.db.save(1, 'дота 2', ['@alice'])
        self.db.save(1, 'сотрудники', ['@bobby'])
        for phrase in ('я иду домой', 'я иду на работу'):
            for name, member in (('дота 2', '@alice'), ('сотрудники', '@bobby')):
                event = self.event(f'{phrase} {name}')
                await self.bot.handle(event)
                self.assertEqual(event.reply.call_args.args[0], member)

    async def test_phrase_buttons_add_edit_duplicate_delete(self):
        await self.click('phrase:add')
        await self.bot.handle(self.event('Я иду на работу', private=True))
        phrase = next(r for r in self.db.phrases(1) if r['text'] == 'Я иду на работу')
        await self.click(f"phrase:edit:{phrase['id']}")
        await self.bot.handle(self.event('Я ИДУ   ДОМОЙ', private=True))
        self.assertEqual(self.db.item(1, 'phrase', phrase['id'])['text'], 'Я иду на работу')
        self.assertIsNotNone(self.db.draft(1))
        await self.bot.handle(self.event('играем', private=True))
        self.assertEqual(self.db.item(1, 'phrase', phrase['id'])['text'], 'играем')
        await self.click(f"phrase:askdelete:{phrase['id']}")
        self.assertIsNotNone(self.db.item(1, 'phrase', phrase['id']))
        await self.click(f"phrase:view:{phrase['id']}")
        self.assertIsNotNone(self.db.item(1, 'phrase', phrase['id']))
        await self.click(f"phrase:delete:{phrase['id']}")
        self.assertIsNone(self.db.item(1, 'phrase', phrase['id']))

    async def test_list_rename_edit_delete_and_stale_buttons(self):
        self.db.register(1)
        self.db.save(1, 'дота 2', ['@alice'])
        item_id = self.db.get(1, 'дота 2')['id']
        await self.click(f'list:rename:{item_id}')
        await self.bot.handle(self.event('сотрудники', private=True))
        self.assertIsNone(self.db.get(1, 'дота 2'))
        self.assertEqual(self.db.get(1, 'сотрудники')['id'], item_id)
        await self.click(f'list:edit:{item_id}')
        await self.bot.handle(self.event('@bobby', private=True))
        self.assertEqual(json.loads(self.db.get(1, 'сотрудники')['members']), ['@bobby'])
        await self.click(f'list:delete:{item_id}')
        self.db.save(1, 'сотрудники', ['@alice'])
        await self.click(f'list:delete:{item_id}')
        self.assertIsNotNone(self.db.get(1, 'сотрудники'))

    async def test_foreign_and_group_callbacks_cannot_modify_data(self):
        self.db.register(1)
        self.db.save(1, 'дота 2', ['@alice'])
        for kind, item_id in [('list', self.db.get(1, 'дота 2')['id']),
                              ('phrase', self.db.phrases(1)[0]['id'])]:
            for action in ('view', 'edit', 'rename', 'delete'):
                await self.click(f'{kind}:{action}:{item_id}', owner=2)
                self.assertIsNone(self.db.draft(2))
                self.assertIsNotNone(self.db.item(1, kind, item_id))
            await self.click(f'{kind}:delete:{item_id}', private=False)
            self.assertIsNotNone(self.db.item(1, kind, item_id))

    async def test_navigation_pagination_and_callback_size(self):
        self.db.register(1)
        for i in range(12):
            self.db.save_phrase(1, 'ф' * 95 + str(i))
        event = await self.click('phrase:page:1')
        buttons = event.reply.call_args.kwargs['buttons']
        self.assertIn('2/2', event.reply.call_args.args[0])
        # Serialize the actual Telethon markup, not a stand-in keyboard type.
        from telethon import TelegramClient
        markup = TelegramClient.build_reply_markup(buttons)
        self.assertTrue(bytes(markup))
        for row in markup.rows:
            for b in row.buttons:
                self.assertLessEqual(len(b.type.data), 64)
        await self.click('phrase:add')
        await self.bot.handle(self.event('Мои списки', private=True))
        self.assertIsNone(self.db.draft(1))

    async def test_overlapping_phrases_fallback_and_longest_match(self):
        self.db.register(1)
        self.db.save_phrase(1, 'я иду домой дота')
        self.db.save(1, 'дота 2', ['@alice'])
        event = self.event('я иду домой дота 2')
        await self.bot.handle(event)
        self.assertEqual(event.reply.call_args.args[0], '@alice')
        self.db.save(1, '2', ['@bobby'])
        event = self.event('я иду домой дота 2')
        await self.bot.handle(event)
        self.assertEqual(event.reply.call_args.args[0], '@bobby')

    async def test_own_lists_only_and_full_phrase_match(self):
        for owner, member in ((1, "@alice"), (2, "@bobby")):
            self.db.register(owner)
            self.db.save(owner, "дота 2", [member])
        event = self.event("Я ИДУ  домой Дота 2", owner=2)
        await self.bot.handle(event)
        self.assertEqual(event.reply.call_args.args[0], "@bobby")
        for text, owner in (("я иду домой дота 2", 3), ("я иду домой дота 2 завтра", 1)):
            event = self.event(text, owner)
            await self.bot.handle(event)
            event.reply.assert_not_awaited()

    async def test_all_everyone_fetches_members_immediately(self):
        users = TotalList([User(id=5, access_hash=99, first_name="Без username")])
        users.total = 1
        self.client.get_participants.return_value = users
        await self.bot.handle(self.event("@all"))
        self.client.get_permissions.assert_not_awaited()
        self.client.get_participants.assert_awaited_once_with(-123, limit=None)
        entity = self.client.send_message.call_args.kwargs["formatting_entities"][0]
        self.assertEqual(entity.user_id.user_id, 5)

    async def test_admin_only_denies_member_allows_admin(self):
        self.config.all_allow_everyone = False
        self.client.get_permissions.return_value = SimpleNamespace(is_admin=False, is_creator=False)
        event = self.event("@all")
        await self.bot.handle(event)
        self.client.get_participants.assert_not_awaited()
        self.assertIn("только администраторам", event.reply.call_args.args[0])
        self.client.get_permissions.return_value = SimpleNamespace(is_admin=True, is_creator=False)
        users = TotalList([])
        users.total = 0
        self.client.get_participants.return_value = users
        await self.bot.handle(self.event("@all"))
        self.client.get_participants.assert_awaited_once()

    async def test_partial_members_warn(self):
        users = TotalList([User(id=5, access_hash=99, first_name="Test")])
        users.total = 500
        self.client.get_participants.return_value = users
        event = self.event("@all")
        await self.bot.handle(event)
        self.assertIn("неполный", event.reply.call_args.args[0])
        self.client.send_message.assert_awaited_once()

    async def test_cooldown_and_concurrent_requests(self):
        self.config.cooldown_seconds = 30
        users = TotalList([])
        users.total = 0
        self.client.get_participants.return_value = users
        await asyncio.gather(self.bot.handle(self.event("@all")), self.bot.handle(self.event("@all")))
        self.client.get_participants.assert_awaited_once()

    async def test_missing_rights_and_floodwait(self):
        self.client.get_participants.side_effect = errors.ChatAdminRequiredError(request=None)
        event = self.event("@all")
        await self.bot.handle(event)
        self.assertIn("администратором", event.reply.call_args.args[0])
        self.client.get_participants.side_effect = errors.FloodWaitError(request=None, capture=60)
        await self.bot.handle(self.event("@all"))
        self.assertGreater(self.bot.blocked_until, 0)
        self.client.get_participants.reset_mock()
        await self.bot.handle(self.event("@all"))
        self.client.get_participants.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

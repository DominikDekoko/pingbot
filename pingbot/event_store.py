"""Persistent reusable automation blocks, event links and execution state."""
import json
from uuid import uuid4


class EventStore:
    def migrate_events(self):
        if self.db.execute('PRAGMA user_version').fetchone()[0] >= 3:
            return
        statements = [
            "CREATE TABLE automation_blocks (id TEXT PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)",
            "CREATE TABLE automation_events (id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'all', enabled INTEGER NOT NULL DEFAULT 0, chance REAL NOT NULL DEFAULT 100, cooldown INTEGER NOT NULL DEFAULT 60, chats TEXT NOT NULL DEFAULT '[]')",
            "CREATE TABLE automation_links (event_id TEXT NOT NULL, block_id TEXT NOT NULL, position INTEGER NOT NULL, PRIMARY KEY(event_id, block_id))",
            "CREATE TABLE automation_seen (chat INTEGER NOT NULL, key TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(chat,key))",
            "CREATE INDEX automation_seen_time ON automation_seen(at)",
            "CREATE TABLE automation_messages (chat INTEGER NOT NULL, message INTEGER NOT NULL, sender INTEGER NOT NULL, at REAL NOT NULL, PRIMARY KEY(chat,message))",
            "CREATE INDEX automation_messages_window ON automation_messages(chat,at,sender)",
            "CREATE INDEX automation_messages_time ON automation_messages(at)",
            "CREATE TABLE automation_runs (event_id TEXT NOT NULL, chat INTEGER NOT NULL, at REAL NOT NULL, PRIMARY KEY(event_id,chat))",
            "CREATE TABLE automation_chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL)",
        ]
        for sql in statements:
            self.db.execute(sql)
        for rule in self.sticker_rules():
            self._import_sticker_event(rule['id'])
        self.db.execute('PRAGMA user_version=3')

    def _import_sticker_event(self, rule_id):
        rule = self.sticker_rule(rule_id)
        condition_id, action_id = uuid4().hex, uuid4().hex
        self.db.execute('INSERT INTO automation_blocks VALUES (?, ?, ?, ?, ?, 1)',
                        (condition_id, 'condition', '@' + rule['username'], 'user', json.dumps({'user_id': rule['target_id']})))
        self.db.execute('INSERT INTO automation_blocks VALUES (?, ?, ?, ?, ?, 1)',
                        (action_id, 'action', 'Стикер @' + rule['username'], 'legacy_sticker', json.dumps({'rule_id': rule_id})))
        self.db.execute("INSERT INTO automation_events VALUES (?, ?, 'all', 1, ?, 0, '[]')",
                        (rule_id, 'Стикер @' + rule['username'], rule['chance']))
        self.db.executemany('INSERT INTO automation_links VALUES (?, ?, ?)',
                            [(rule_id, condition_id, 0), (rule_id, action_id, 1)])

    def blocks(self, category):
        return self.db.execute('SELECT * FROM automation_blocks WHERE category=? ORDER BY rowid', (category,)).fetchall()

    def block(self, item_id):
        return self.db.execute('SELECT * FROM automation_blocks WHERE id=?', (item_id,)).fetchone()

    def put_block(self, category, name, kind, data, item_id=None):
        with self.db:
            if item_id:
                if not self.db.execute('UPDATE automation_blocks SET name=?, data=? WHERE id=? AND category=? AND kind=?',
                                       (name, json.dumps(data), item_id, category, kind)).rowcount:
                    raise ValueError('Элемент удалён или изменён другим администратором.')
            else:
                item_id = uuid4().hex
                self.db.execute('INSERT INTO automation_blocks VALUES (?, ?, ?, ?, ?, 1)',
                                (item_id, category, name, kind, json.dumps(data)))
        return item_id

    def event_rules(self):
        return self.db.execute('SELECT * FROM automation_events ORDER BY rowid').fetchall()

    def event_rule(self, item_id):
        return self.db.execute('SELECT * FROM automation_events WHERE id=?', (item_id,)).fetchone()

    def create_event(self, name):
        item_id = uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO automation_events(id,name) VALUES (?,?)', (item_id, name))
        return item_id

    def change_event(self, item_id, field, value):
        if field not in {'name', 'mode', 'enabled', 'chance', 'cooldown', 'chats'}:
            raise ValueError('Unknown event setting')
        with self.db:
            if not self.db.execute(f'UPDATE automation_events SET {field}=? WHERE id=?', (value, item_id)).rowcount:
                raise ValueError('Событие уже удалено.')
            if field == 'chance':
                self.db.execute('UPDATE sticker_rules SET chance=? WHERE id=?', (value, item_id))

    def linked_blocks(self, event_id):
        return self.db.execute('SELECT b.* FROM automation_blocks b JOIN automation_links l ON b.id=l.block_id '
                               'WHERE l.event_id=? ORDER BY l.position', (event_id,)).fetchall()

    def toggle_link(self, event_id, block_id):
        with self.db:
            if not self.event_rule(event_id) or not self.block(block_id):
                raise ValueError('Событие или элемент уже удалён.')
            # Changing composition requires explicit re-enabling, avoiding interim rules.
            self.db.execute('UPDATE automation_events SET enabled=0 WHERE id=?', (event_id,))
            if self.db.execute('DELETE FROM automation_links WHERE event_id=? AND block_id=?', (event_id, block_id)).rowcount:
                return
            category = self.block(block_id)['category']
            self.db.execute('DELETE FROM automation_links WHERE event_id=? AND block_id IN '
                            '(SELECT id FROM automation_blocks WHERE category=?)', (event_id, category))
            position = self.db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM automation_links WHERE event_id=?', (event_id,)).fetchone()[0]
            self.db.execute('INSERT INTO automation_links VALUES (?,?,?)', (event_id, block_id, position))

    def toggle_block(self, item_id):
        with self.db:
            self.db.execute('UPDATE automation_blocks SET enabled=1-enabled WHERE id=?', (item_id,))

    def delete_automation(self, category, item_id):
        with self.db:
            if category == 'event':
                self.db.execute('DELETE FROM automation_events WHERE id=?', (item_id,))
                self.db.execute('DELETE FROM automation_links WHERE event_id=?', (item_id,))
                self.db.execute('DELETE FROM automation_runs WHERE event_id=?', (item_id,))
            else:
                # Never silently broaden an active rule by deleting its condition.
                self.db.execute('UPDATE automation_events SET enabled=0 WHERE id IN '
                                '(SELECT event_id FROM automation_links WHERE block_id=?)', (item_id,))
                self.db.execute('DELETE FROM automation_links WHERE block_id=?', (item_id,))
                self.db.execute('DELETE FROM automation_blocks WHERE id=? AND category=?', (item_id, category))

    def observe(self, chat, key, sender, message_id, now, kind):
        with self.db:
            self.db.execute('DELETE FROM automation_seen WHERE at<?', (now - 86400,))
            self.db.execute('DELETE FROM automation_messages WHERE at<?', (now - 86400,))
            if kind != 'message':
                # ChatAction and raw participant updates may describe the same change.
                previous = self.db.execute('SELECT at FROM automation_seen WHERE chat=? AND key=?', (chat, key)).fetchone()
                if previous and now - previous['at'] < 3:
                    return False
                self.db.execute('DELETE FROM automation_seen WHERE chat=? AND key=?', (chat, key))
            added = self.db.execute('INSERT OR IGNORE INTO automation_seen VALUES (?,?,?)', (chat, key, now)).rowcount
            if added and kind == 'message':
                self.db.execute('INSERT OR IGNORE INTO automation_messages VALUES (?,?,?,?)', (chat, message_id, sender, now))
            return bool(added)

    def count_messages(self, chat, sender, since):
        return self.db.execute('SELECT COUNT(*) FROM automation_messages WHERE chat=? AND at>? AND (? IS NULL OR sender=?)',
                               (chat, since, sender, sender)).fetchone()[0]

    def last_run(self, event_id, chat):
        row = self.db.execute('SELECT at FROM automation_runs WHERE event_id=? AND chat=?', (event_id, chat)).fetchone()
        return row['at'] if row else float('-inf')

    def record_run(self, event_id, chat, now):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO automation_runs VALUES (?,?,?)', (event_id, chat, now))

    def remember_chat(self, chat_id, title):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO automation_chats VALUES (?,?)', (chat_id, title))

    def known_chats(self):
        return self.db.execute('SELECT * FROM automation_chats ORDER BY title').fetchall()

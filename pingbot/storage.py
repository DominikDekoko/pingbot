import json
import sqlite3
from uuid import uuid4
from .event_store import EventStore


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


class Storage(EventStore):
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, phrase TEXT NOT NULL DEFAULT 'я иду домой'
            );
            CREATE TABLE IF NOT EXISTS lists (
                owner INTEGER NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL,
                members TEXT NOT NULL, PRIMARY KEY(owner, name_key)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                owner INTEGER PRIMARY KEY, stage TEXT NOT NULL, name TEXT
            );
        """)
        # One-time migration: preserve each existing user's phrase and lists.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
                self.db.execute("CREATE TABLE phrases (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, "
                                "text TEXT NOT NULL, text_key TEXT NOT NULL, UNIQUE(owner, text_key))")
                self.db.execute("ALTER TABLE lists ADD COLUMN id TEXT")
                for row in self.db.execute("SELECT owner, name_key FROM lists").fetchall():
                    self.db.execute("UPDATE lists SET id=? WHERE owner=? AND name_key=?",
                                    (uuid4().hex, row['owner'], row['name_key']))
                self.db.execute("CREATE UNIQUE INDEX lists_id ON lists(id)")
                for row in self.db.execute("SELECT id, phrase FROM users").fetchall():
                    self.db.execute("INSERT INTO phrases VALUES (?, ?, ?, ?)",
                                    (uuid4().hex, row['id'], row['phrase'], normalize(row['phrase'])))
                self.db.execute("PRAGMA user_version=1")
            if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
                self.db.execute("""CREATE TABLE sticker_rules (
                    id TEXT PRIMARY KEY, target_id INTEGER NOT NULL UNIQUE,
                    username TEXT NOT NULL, chance REAL NOT NULL CHECK(chance >= 0 AND chance <= 100),
                    document BLOB NOT NULL, content BLOB NOT NULL
                )""")
                self.db.execute("PRAGMA user_version=2")
            self.migrate_events()

    def sticker_rules(self):
        return self.db.execute("SELECT id, target_id, username, chance FROM sticker_rules ORDER BY username").fetchall()

    def sticker_rule(self, item_id):
        return self.db.execute("SELECT * FROM sticker_rules WHERE id=?", (item_id,)).fetchone()

    def sticker_for_user(self, target_id):
        return self.db.execute("SELECT * FROM sticker_rules WHERE target_id=?", (target_id,)).fetchone()

    def save_sticker_rule(self, owner, target_id, username, chance, document, content):
        item_id = uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO sticker_rules VALUES (?, ?, ?, ?, ?, ?)",
                            (item_id, target_id, username, chance, document, content))
            self._import_sticker_event(item_id)
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))
        return item_id

    def update_sticker_rule(self, owner, item_id, *, chance=None, document=None, content=None):
        with self.db:
            if chance is not None:
                changed = self.db.execute("UPDATE sticker_rules SET chance=? WHERE id=?", (chance, item_id)).rowcount
                self.db.execute('UPDATE automation_events SET chance=? WHERE id=?', (chance, item_id))
            else:
                changed = self.db.execute("UPDATE sticker_rules SET document=?, content=? WHERE id=?",
                                         (document, content, item_id)).rowcount
            if not changed:
                raise ValueError("Правило уже удалено. Откройте меню заново.")
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def delete_sticker_rule(self, item_id):
        with self.db:
            self.db.execute("DELETE FROM sticker_rules WHERE id=?", (item_id,))
            self.db.execute('UPDATE automation_events SET enabled=0 WHERE id=?', (item_id,))
            for action in self.blocks('action'):
                if action['kind'] == 'legacy_sticker' and json.loads(action['data'])['rule_id'] == item_id:
                    self.db.execute('UPDATE automation_events SET enabled=0 WHERE id IN '
                                    '(SELECT event_id FROM automation_links WHERE block_id=?)', (action['id'],))
                    self.db.execute('DELETE FROM automation_links WHERE block_id=?', (action['id'],))
                    self.db.execute('DELETE FROM automation_blocks WHERE id=?', (action['id'],))

    def refresh_sticker_document(self, item_id, previous, refreshed):
        with self.db:
            # Do not overwrite a sticker replaced by an administrator meanwhile.
            self.db.execute("UPDATE sticker_rules SET document=? WHERE id=? AND document=?",
                            (refreshed, item_id, previous))

    def register(self, owner):
        with self.db:
            created = self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (owner,)).rowcount
            if created:
                self.db.execute("INSERT INTO phrases VALUES (?, ?, ?, ?)",
                                (uuid4().hex, owner, 'я иду домой', 'я иду домой'))

    def phrases(self, owner):
        return self.db.execute("SELECT * FROM phrases WHERE owner=? ORDER BY text_key", (owner,)).fetchall()

    def save_phrase(self, owner, text, item_id=None):
        with self.db:
            if item_id:
                changed = self.db.execute("UPDATE phrases SET text=?, text_key=? WHERE owner=? AND id=?",
                                         (text, normalize(text), owner, item_id)).rowcount
                if not changed:
                    raise ValueError("Фраза уже удалена. Откройте меню заново.")
            else:
                item_id = uuid4().hex
                self.db.execute("INSERT INTO phrases VALUES (?, ?, ?, ?)",
                                (item_id, owner, text, normalize(text)))
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))
        return item_id

    def item(self, owner, kind, item_id):
        table = {'list': 'lists', 'phrase': 'phrases'}[kind]
        return self.db.execute(f"SELECT * FROM {table} WHERE owner=? AND id=?", (owner, item_id)).fetchone()

    def delete_item(self, owner, kind, item_id):
        table = {'list': 'lists', 'phrase': 'phrases'}[kind]
        with self.db:
            return self.db.execute(f"DELETE FROM {table} WHERE owner=? AND id=?", (owner, item_id)).rowcount > 0

    def rename_list(self, owner, item_id, name):
        with self.db:
            changed = self.db.execute("UPDATE lists SET name=?, name_key=? WHERE owner=? AND id=?",
                                     (name, normalize(name), owner, item_id)).rowcount
            if not changed:
                raise ValueError("Список уже удалён. Откройте меню заново.")
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def replace_members(self, owner, item_id, members):
        with self.db:
            changed = self.db.execute("UPDATE lists SET members=? WHERE owner=? AND id=?",
                                     (json.dumps(members), owner, item_id)).rowcount
            if not changed:
                raise ValueError("Список уже удалён. Откройте меню заново.")
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def lists(self, owner):
        return self.db.execute("SELECT * FROM lists WHERE owner=? ORDER BY name_key", (owner,)).fetchall()

    def get(self, owner, name):
        return self.db.execute("SELECT * FROM lists WHERE owner=? AND name_key=?",
                               (owner, normalize(name))).fetchone()

    def save(self, owner, name, members):
        with self.db:
            self.db.execute("INSERT INTO lists(owner, name, name_key, members, id) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(owner, name_key) DO UPDATE SET name=excluded.name, members=excluded.members",
                            (owner, name, normalize(name), json.dumps(members), uuid4().hex))
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def delete(self, owner, name):
        with self.db:
            return self.db.execute("DELETE FROM lists WHERE owner=? AND name_key=?",
                                   (owner, normalize(name))).rowcount > 0

    def draft(self, owner):
        return self.db.execute("SELECT * FROM drafts WHERE owner=?", (owner,)).fetchone()

    def set_draft(self, owner, stage, name=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO drafts VALUES (?, ?, ?)", (owner, stage, name))

    def cancel(self, owner):
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE owner=?", (owner,))

    def close(self):
        self.db.close()

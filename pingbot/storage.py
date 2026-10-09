import json
import sqlite3


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


class Storage:
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

    def register(self, owner):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(id) VALUES (?)", (owner,))

    def phrase(self, owner):
        row = self.db.execute("SELECT phrase FROM users WHERE id=?", (owner,)).fetchone()
        return row[0] if row else None

    def set_phrase(self, owner, phrase):
        with self.db:
            self.db.execute("UPDATE users SET phrase=? WHERE id=?", (phrase, owner))

    def lists(self, owner):
        return self.db.execute("SELECT * FROM lists WHERE owner=? ORDER BY name_key", (owner,)).fetchall()

    def get(self, owner, name):
        return self.db.execute("SELECT * FROM lists WHERE owner=? AND name_key=?",
                               (owner, normalize(name))).fetchone()

    def save(self, owner, name, members):
        with self.db:
            self.db.execute("INSERT INTO lists VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(owner, name_key) DO UPDATE SET name=excluded.name, members=excluded.members",
                            (owner, name, normalize(name), json.dumps(members)))
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

import re

from telethon import utils
from telethon.tl.types import InputMessageEntityMentionName


ALL_PATTERN = re.compile(r"(?<![\w@/])@all(?!\w)", re.IGNORECASE)


def parse_members(text):
    parts = text.split()
    if not parts or len(parts) > 200:
        raise ValueError("Отправьте от 1 до 200 @username через пробел.")
    if any(not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{0,31}", part) for part in parts):
        raise ValueError("Нужны @username через пробел, без ссылок и запятых.")
    return list(dict.fromkeys(part.lower() for part in parts))


def username_batches(members, size=20):
    for i in range(0, len(members), size):
        yield " ".join(members[i:i + size])


def user_batches(users, size=20):
    text = ""
    entities = []
    seen = set()
    for user in users:
        if user.id in seen or user.bot or user.deleted:
            continue
        seen.add(user.id)
        name = " ".join((utils.get_display_name(user) or str(user.id)).split())[:80]
        if not name:
            name = str(user.id)
        if text:
            text += ", "
        offset = len(text.encode("utf-16-le")) // 2
        text += name
        entities.append(InputMessageEntityMentionName(
            offset, len(name.encode("utf-16-le")) // 2, utils.get_input_user(user)))
        if len(entities) >= size:
            yield text, entities
            text, entities = "", []
    if entities:
        yield text, entities

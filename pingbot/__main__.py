import asyncio
import logging
import signal

from telethon import TelegramClient, connection, events

from .bot import Bot
from .config import Config
from .storage import Storage


async def main():
    config = Config.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    storage = Storage(config.data_dir / "pingbot.sqlite3")
    client = TelegramClient(
        str(config.data_dir / "bot"), config.api_id, config.api_hash,
        connection=connection.ConnectionTcpMTProxyRandomizedIntermediate,
        # Explicit prefix prevents Telethon stripping dd/ee from a raw random key.
        proxy=(config.proxy_host, config.proxy_port, "dd" + config.proxy_secret),
        flood_sleep_threshold=0,
    )
    try:
        await client.start(bot_token=config.bot_token)
        me = await client.get_me()
        if not me.bot or me.id != int(config.bot_token.split(":", 1)[0]):
            raise ValueError("Сессия принадлежит другому аккаунту. Используйте отдельный DATA_DIR для этого бота.")
        bot = Bot(client, storage, config, me.username)
        client.add_event_handler(bot.handle, events.NewMessage(incoming=True))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(client.disconnect()))
        logging.info("Bot @%s started; ALL_ALLOW_EVERYONE=%s", me.username, config.all_allow_everyone)
        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        storage.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())

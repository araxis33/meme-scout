import asyncio
import logging
import signal

import bot
import config
import db
from watchers import discovery, pump_dump

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.BASE_DIR / "meme-scout-log.txt", encoding="utf-8"),
    ],
)
log = logging.getLogger("meme-scout.main")


async def main():
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы - заполни .env (см. .env.example)")

    db.init_db()
    application = bot.build_application()

    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    log.info("Telegram bot polling started")

    stop_event = asyncio.Event()

    def _request_stop():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass  # signal handlers via add_signal_handler aren't available on Windows

    tasks = [
        asyncio.create_task(discovery.run_discovery_base(application)),
        asyncio.create_task(discovery.run_discovery_robinhood(application)),
        asyncio.create_task(pump_dump.run_pump_dump_watcher(application)),
    ]

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

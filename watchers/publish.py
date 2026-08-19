"""Раз в час выкладывает свежий снимок статистики на сайт.

Сам бот личный и никуда не переезжает, а вот его цифры показываются всем
на deftools.xyz. Страница статическая, поэтому переживает и выключенный
ноутбук - просто честно пишет, насколько данные стары.
"""
import asyncio
import logging
from pathlib import Path

import config
import export_stats

log = logging.getLogger("meme-scout.publish")


async def run_publisher(application):
    if not config.PUBLISH_ENABLED:
        log.info("Stats publishing disabled via config")
        return

    repo = Path(config.SITE_REPO_PATH)
    if not repo.exists():
        log.warning("Site repo not found at %s - stats publishing is off", repo)
        return

    while True:
        try:
            stats = await asyncio.to_thread(export_stats.build_stats)
            # git и запись на диск блокирующие - уводим в поток,
            # иначе на время пуша встают все остальные вотчеры.
            await asyncio.to_thread(export_stats.publish, repo, stats)
        except Exception:
            log.exception("Stats publish failed")
        await asyncio.sleep(config.PUBLISH_INTERVAL_SECONDS)

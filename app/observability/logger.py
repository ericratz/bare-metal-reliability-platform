import logging

from pythonjsonlogger import jsonlogger

from app.core.settings import settings


class _IdentityFilter(logging.Filter):
    """
    Stamps node and version onto every record.

    Both app instances log to their own journal, and the health monitor and any
    manual `journalctl` digging happen per-node — so a line pulled out of
    context needs to say which node and which image version produced it.
    """

    def filter(self, record):
        record.node = settings.NODE_NAME
        record.version = settings.APP_VERSION
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(node)s %(version)s"
        )
        handler.setFormatter(formatter)
        handler.addFilter(_IdentityFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        #uvicorn configures the root logger; without this every line is emitted
        #twice, once as JSON and once in uvicorn's plain format.
        logger.propagate = False
    return logger

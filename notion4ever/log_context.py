from __future__ import annotations

import logging
from contextvars import ContextVar

ROOT_PREFIX: ContextVar[str] = ContextVar("ROOT_PREFIX", default="-")
CURRENT_PAGE: ContextVar[str | None] = ContextVar("CURRENT_PAGE", default=None)


class PageContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        root = ROOT_PREFIX.get()
        page = CURRENT_PAGE.get()

        # Эти поля будут доступны в format="...%(page_prefix)s..."
        if page:
            record.page_prefix = f"{root} | {page}"
        else:
            record.page_prefix = root

        return True


def install_log_record_factory() -> None:
    """
    Guarantee that every LogRecord has 'page_prefix',
    even if filters weren't applied (e.g., 3rd-party loggers).
    """
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        if not hasattr(record, "page_prefix"):
            record.page_prefix = ROOT_PREFIX.get()
        return record

    logging.setLogRecordFactory(record_factory)

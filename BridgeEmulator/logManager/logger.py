import logging
import logging.handlers
import os
import re
import sys
import threading
from collections import deque

# The status page tails the log, so keep the most recent records addressable
# without re-reading (and re-parsing) the rotating log file.
RING_BUFFER_SIZE = 500

# Werkzeug colours its access log, which renders as escape sequences in the
# browser.
_ANSI = re.compile(r'\x1b\[[0-9;]*m')


def _get_log_format():
    return logging.Formatter('%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s')


def _get_log_file():
    # The file handler used to be given a bare relative filename, which put the
    # log wherever the process happened to be started from. Honour an explicit
    # override so packaging and tests can place it deliberately.
    return os.environ.get('DIYHUE_LOG_FILE', 'diyhue.log')


class RingBufferHandler(logging.Handler):
    """Keeps the last ``capacity`` records so they can be served over the API."""

    def __init__(self, capacity=RING_BUFFER_SIZE):
        super().__init__()
        self.records = deque(maxlen=capacity)
        self._lock_records = threading.Lock()

    def emit(self, record):
        try:
            entry = {
                "time": self.formatter.formatTime(record) if self.formatter else "",
                "level": record.levelname,
                "levelno": record.levelno,
                "name": record.name,
                "message": _ANSI.sub("", record.getMessage()),
            }
            if record.exc_info:
                entry["message"] += "\n" + _ANSI.sub("", self.format(record)).split("\n", 1)[-1]
        except Exception:
            self.handleError(record)
            return
        with self._lock_records:
            self.records.append(entry)

    def tail(self, limit=100, min_level=logging.INFO):
        with self._lock_records:
            entries = [entry for entry in self.records if entry["levelno"] >= min_level]
        return entries[-limit:]


class Logger:
    loggers = {}
    logLevel = logging.DEBUG # Capture all logs prior to switching
    ringBuffer = RingBufferHandler()

    def configure_logger(self, level):
        self.logLevel = getattr(logging, level)

        for loggerName in self.loggers:
            self.loggers[loggerName].handlers.clear()
            self._setup_logger(loggerName)

    def _setup_logger(self, name):
        logger = logging.getLogger(name)

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_get_log_format())
        handler.setLevel(logging.DEBUG)
        handler.addFilter(lambda record: record.levelno <= logging.INFO)
        logger.addHandler(handler)

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_get_log_format())
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)

        handler = logging.handlers.RotatingFileHandler(filename=_get_log_file(), maxBytes=(10000000), backupCount=7)
        handler.setFormatter(_get_log_format())
        handler.setLevel(logging.DEBUG)
        handler.addFilter(lambda record: record.levelno <= logging.CRITICAL)
        logger.addHandler(handler)

        self.ringBuffer.setFormatter(_get_log_format())
        self.ringBuffer.setLevel(logging.DEBUG)
        logger.addHandler(self.ringBuffer)

        logger.setLevel(self.logLevel)
        logger.propagate = False
        return logger

    def get_logger(self, name):
        if name not in self.loggers:
            self.loggers[name] = self._setup_logger(name)
        return self.loggers[name]

    def get_recent(self, limit=100, level="INFO"):
        """Most recent log records at or above ``level``, oldest first."""
        return self.ringBuffer.tail(limit=limit, min_level=getattr(logging, level, logging.INFO))

    def get_level_name(self):
        INFO = 20
        DEBUG = 10

        _levelToName = {
            INFO: 'INFO',
            DEBUG: 'DEBUG',
        }
        return _levelToName.get(self.logLevel)

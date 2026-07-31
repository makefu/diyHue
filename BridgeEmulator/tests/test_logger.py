"""The log tail that backs the status page."""

import logging

import logManager
# logManager rebinds its own `logger` attribute to a Logger instance, so the
# class has to come from the submodule itself.
from logManager.logger import RingBufferHandler


def _record(message, level=logging.INFO, name="test"):
    return logging.LogRecord(name, level, "test.py", 1, message, None, None)


class TestRingBuffer:
    def test_records_are_retained_in_order(self):
        buffer = RingBufferHandler(capacity=3)
        buffer.setFormatter(logging.Formatter('%(message)s'))
        for index in range(5):
            buffer.emit(_record(f"line {index}"))

        assert [entry["message"] for entry in buffer.tail()] == ["line 2", "line 3", "line 4"]

    def test_tail_filters_by_level(self):
        buffer = RingBufferHandler()
        buffer.setFormatter(logging.Formatter('%(message)s'))
        buffer.emit(_record("chatty", level=logging.DEBUG))
        buffer.emit(_record("important", level=logging.WARNING))

        assert [entry["message"] for entry in buffer.tail(min_level=logging.INFO)] == ["important"]
        assert len(buffer.tail(min_level=logging.DEBUG)) == 2

    def test_colour_codes_are_stripped(self):
        """Werkzeug colours its access log; the browser would show the escapes."""
        buffer = RingBufferHandler()
        buffer.setFormatter(logging.Formatter('%(message)s'))
        buffer.emit(_record('\x1b[36mGET /status/ HTTP/1.1\x1b[0m 200'))

        assert buffer.tail()[0]["message"] == 'GET /status/ HTTP/1.1 200'

    def test_shared_buffer_captures_real_loggers(self):
        logger = logManager.logger.get_logger("tests.ringbuffer")
        logger.info("hello from a real logger")

        messages = [entry["message"] for entry in logManager.logger.get_recent(limit=50)]
        assert "hello from a real logger" in messages

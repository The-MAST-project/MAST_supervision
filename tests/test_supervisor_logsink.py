import logging

from supervision.logsink import DequeHandler, LogLine


def _record(msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("mast.test", level, __file__, 1, msg, None, None)


def test_is_not_a_stream_handler() -> None:
    # init_log adds the console only when no StreamHandler is attached; one here would hide it.
    assert not isinstance(DequeHandler(maxlen=1), logging.StreamHandler)


def test_formats_on_the_producing_thread() -> None:
    handler = DequeHandler(maxlen=10)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.emit(_record("hello", logging.WARNING))
    assert list(handler.lines) == [LogLine(logging.WARNING, "WARNING hello")]


def test_is_bounded_keeping_the_newest() -> None:
    handler = DequeHandler(maxlen=3)
    for i in range(5):
        handler.emit(_record(f"m{i}"))
    assert [line.text for line in handler.lines] == ["m2", "m3", "m4"]

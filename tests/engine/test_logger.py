"""Unit tests for koval.engine.logger."""

import logging
import queue

from koval.engine.logger import (
    PROJECT_ROOT_LOGGER,
    QueueHandler,
    WsFormatter,
    clear_ws_log_queue,
    coerce_log_level,
    get_logger,
    setup_logging,
    ws_log_queue,
)


def teardown_function():
    logging.getLogger(PROJECT_ROOT_LOGGER).handlers.clear()
    clear_ws_log_queue()


def test_get_logger_uses_koval_namespace():
    logger = get_logger("engine.data_loader")

    assert isinstance(logger, logging.Logger)
    assert logger.name == "koval.engine.data_loader"


def test_get_logger_does_not_duplicate_fully_qualified_namespace():
    logger = get_logger("koval.engine.data_loader")

    assert logger.name == "koval.engine.data_loader"


def test_setup_logging_configures_non_propagating_project_logger():
    setup_logging(level="debug", enable_ws=False)

    root = logging.getLogger(PROJECT_ROOT_LOGGER)

    assert root.level == logging.DEBUG
    assert root.propagate is False
    assert len(root.handlers) == 1


def test_setup_logging_with_ws_adds_queue_handler_and_respects_level_override():
    setup_logging(level="debug", ws_level="info", enable_ws=True)

    root = logging.getLogger(PROJECT_ROOT_LOGGER)
    queue_handler = next(handler for handler in root.handlers if isinstance(handler, QueueHandler))

    assert root.level == logging.DEBUG
    assert queue_handler.level == logging.INFO


def test_queue_handler_puts_formatted_message_to_queue():
    target_queue = queue.Queue()
    handler = QueueHandler(target_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)

    handler.emit(record)

    assert target_queue.get_nowait() == "hello"


def test_queue_handler_drops_oldest_when_queue_is_full():
    target_queue = queue.Queue(maxsize=1)
    handler = QueueHandler(target_queue)
    handler.setFormatter(logging.Formatter("%(message)s"))

    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "first", (), None))
    handler.emit(logging.LogRecord("test", logging.INFO, "", 0, "second", (), None))

    assert target_queue.get_nowait() == "second"


def test_clear_ws_log_queue_drains_messages():
    ws_log_queue.put_nowait("line-1")
    ws_log_queue.put_nowait("line-2")

    removed = clear_ws_log_queue()

    assert removed == 2
    assert ws_log_queue.empty()


def test_ws_formatter_uses_default_prefix_and_record_override():
    formatter = WsFormatter(prefix="[run-1] ")
    default_record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    override_record = logging.LogRecord("test", logging.INFO, "", 0, "[LIVE] Starting", (), None)
    override_record.ws_prefix_override = ""

    assert formatter.format(default_record) == "[run-1] hello"
    assert formatter.format(override_record) == "[LIVE] Starting"


def test_coerce_log_level_normalizes_strings_and_falls_back():
    assert coerce_log_level("warning") == logging.WARNING
    assert coerce_log_level("not-a-level", default=logging.ERROR) == logging.ERROR

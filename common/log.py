"""Compact logging configured explicitly by the application entry point."""

import copy
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Dict, List, Optional, Type, Union


class CustomFormatter(logging.Formatter):
    """Format a compact prefix while retaining exception and stack details."""

    LEVEL_MAP = {
        logging.DEBUG: "D",
        logging.INFO: "I",
        logging.WARNING: "W",
        logging.ERROR: "E",
        logging.CRITICAL: "C",
    }

    def __init__(self) -> None:
        super().__init__(
            "%(level_prefix)s%(asctime)s %(process)s:%(thread)s "
            "%(filename)s:%(funcName)s:%(lineno)d] %(message)s"
        )

    def formatTime(
        self, record: logging.LogRecord, datefmt: Optional[str] = None
    ) -> str:
        timestamp = datetime.fromtimestamp(record.created).astimezone()
        return (
            timestamp.strftime("%Y-%m-%d %H:%M:%S")
            + ".{:03d}".format(int(record.msecs))
            + timestamp.strftime("%z")
        )

    def format(self, record: logging.LogRecord) -> str:
        local_record = copy.copy(record)
        local_record.level_prefix = self.LEVEL_MAP.get(record.levelno, "U")
        if local_record.process is None:
            local_record.process = "-"
        if local_record.thread is None:
            local_record.thread = "-"
        return super().format(local_record)


@dataclass
class _State:
    logger: logging.Logger
    level: int
    to_console: bool
    directory: Optional[Path]
    file_path: Optional[Path]
    handlers: List[logging.Handler]
    previous_level: int
    previous_propagate: bool
    users: int = 1


_states: Dict[str, _State] = {}


def _validate_level(level: int) -> None:
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        raise ValueError("Log level must be a non-negative integer")


class Log:
    """Configure a named logger with optional console and file output.

    Configure, change levels, and close from the application control thread.
    Worker threads may emit through the returned standard-library logger.
    Identical configurations share handlers until the final wrapper closes.
    Each process must initialize logging after process creation.
    """

    debug = logging.DEBUG
    info = logging.INFO
    warning = logging.WARNING
    error = logging.ERROR
    critical = logging.CRITICAL

    def __init__(
        self,
        name: str,
        level: int = logging.INFO,
        to_console: bool = True,
        path: Optional[Union[str, os.PathLike]] = None,
    ) -> None:
        if not isinstance(name, str) or not name or name == "root" or any(
            char in "/\\" or ord(char) < 32 for char in name
        ):
            raise ValueError(
                "Log name must be nonempty, not 'root', and contain no "
                "path separators or controls"
            )
        _validate_level(level)
        directory = (
            Path(path).expanduser().resolve() if path is not None else None
        )
        state = _states.get(name)
        if state is not None:
            if (state.level, state.to_console, state.directory) != (
                level, to_console, directory
            ):
                raise ValueError(
                    "Logger {!r} already has a different configuration".format(
                        name
                    )
                )
            state.users += 1
        else:
            logger = logging.getLogger(name)
            if logger.handlers:
                raise ValueError(
                    "Logger {!r} already has unmanaged handlers".format(name)
                )
            handlers: List[logging.Handler] = []
            file_path = None
            try:
                if to_console:
                    handlers.append(logging.StreamHandler(sys.stderr))
                if directory is not None:
                    directory.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d.%H%M%S.%f")
                    file_path = directory / "{}.{}.{}.log".format(
                        name, stamp, os.getpid()
                    )
                    handlers.append(
                        logging.FileHandler(
                            file_path, mode="x", encoding="utf-8"
                        )
                    )
                if not handlers:
                    # Prevent lastResort from writing disabled output.
                    handlers.append(logging.NullHandler())
                formatter = CustomFormatter()
                for handler in handlers:
                    handler.setFormatter(formatter)
            except Exception:
                for handler in handlers:
                    handler.close()
                raise
            state = _State(
                logger, level, to_console, directory, file_path, handlers,
                logger.level, logger.propagate,
            )
            logger.setLevel(level)
            logger.propagate = False
            for handler in handlers:
                logger.addHandler(handler)
            _states[name] = state
        self._state = state
        self._closed = False

    @property
    def file_path(self) -> Optional[Path]:
        """Return the output file path, or None if disabled."""
        return self._state.file_path

    def get_logger(self) -> logging.Logger:
        """Return the underlying logger while this wrapper is open."""
        if self._closed:
            raise RuntimeError("Log has been closed")
        return self._state.logger

    def set_level(self, level: int) -> None:
        """Change the level for all wrappers sharing this named logger."""
        _validate_level(level)
        self.get_logger().setLevel(level)
        self._state.level = level

    def close(self) -> None:
        """Release this wrapper; the last user closes all managed handlers."""
        if self._closed:
            return
        self._closed = True
        state = self._state
        state.users -= 1
        if state.users:
            return
        del _states[state.logger.name]
        for handler in state.handlers:
            state.logger.removeHandler(handler)
        state.logger.setLevel(state.previous_level)
        state.logger.propagate = state.previous_propagate
        first_error: Optional[Exception] = None
        for handler in state.handlers:
            for operation in (handler.flush, handler.close):
                try:
                    operation()
                except Exception as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "Log":
        self.get_logger()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        try:
            self.close()
        except Exception as error:
            if exc_value is None:
                raise
            # Report outside logging: its handlers have already been detached.
            # A broken diagnostic stream must not replace the body exception.
            for stream in (sys.stderr, sys.__stderr__):
                if stream is None:
                    continue
                try:
                    print(
                        "Failed to close logger {!r}: {}: {}".format(
                            self._state.logger.name,
                            type(error).__name__,
                            error,
                        ),
                        file=stream,
                        flush=True,
                    )
                    break
                except Exception:
                    continue

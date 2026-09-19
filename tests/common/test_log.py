"""Behavior tests for the shared logging wrapper."""

import io
import logging
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from common.log import CustomFormatter, Log


class LogTest(unittest.TestCase):
    def make_log(self, **kwargs):
        log = Log(self.id(), **kwargs)
        self.addCleanup(log.close)
        return log

    def test_console_format_filter_and_child_logger(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            log = self.make_log()
            logger = logging.getLogger(self.id() + ".reader")
            logger.info("rows=%d", 12)
            logger.debug("hidden")
            log.set_level(Log.debug)
            logger.debug("visible")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(
            lines[0],
            r"^I\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} "
            r"\d+:\d+ test_log.py:"
            r"test_console_format_filter_and_child_logger:\d+\] rows=12$",
        )
        self.assertTrue(lines[1].startswith("D"))
        self.assertTrue(lines[1].endswith("visible"))

    def test_exception_chain_and_stack_info(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            logger = self.make_log().get_logger()
            try:
                try:
                    raise ValueError("source failure")
                except ValueError as cause:
                    raise RuntimeError("extraction failed") from cause
            except RuntimeError:
                logger.exception("failed")
            logger.info("stack", stack_info=True)
        result = output.getvalue()
        self.assertIn("Traceback (most recent call last)", result)
        self.assertIn("ValueError: source failure", result)
        self.assertIn("RuntimeError: extraction failed", result)
        self.assertIn("Stack (most recent call last)", result)

    def test_file_output_and_release(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "logs"
            log = self.make_log(to_console=False, path=directory)
            logger = log.get_logger()
            handler = logger.handlers[0]
            stream = handler.stream
            logger.info("数据 rows=%d", 3)
            log.close()
            log.close()
            self.assertTrue(stream.closed)
            self.assertNotIn(handler, logger.handlers)
            files = list(directory.iterdir())
            self.assertEqual(len(files), 1)
            self.assertRegex(
                files[0].name, r"\.\d{8}\.\d{6}\.\d{6}\.\d+\.log$"
            )
            self.assertIn("数据 rows=3", files[0].read_text(encoding="utf-8"))
            with self.assertRaises(RuntimeError):
                log.get_logger()

    def test_same_configuration_reuses_output_until_last_close(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            first = self.make_log()
            second = self.make_log()
            self.assertIs(first.get_logger(), second.get_logger())
            self.assertEqual(len(first.get_logger().handlers), 1)
            first.close()
            second.get_logger().info("once")
            second.close()
        self.assertEqual(output.getvalue().count("once"), 1)

    def test_conflicting_configuration_does_not_change_active_logger(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            first = self.make_log()
            with self.assertRaises(ValueError):
                Log(self.id(), level=Log.debug)
            first.get_logger().info("still configured")
        self.assertIn("still configured", output.getvalue())

    def test_existing_handlers_and_root_are_preserved(self):
        logger = logging.getLogger(self.id())
        existing = logging.NullHandler()
        logger.addHandler(existing)
        self.addCleanup(logger.removeHandler, existing)
        with self.assertRaises(ValueError):
            Log(self.id())
        self.assertEqual(logger.handlers, [existing])
        root = logging.getLogger()
        before = (root.level, list(root.handlers))
        other = Log(self.id() + ".isolated", to_console=False)
        self.addCleanup(other.close)
        self.assertEqual((root.level, root.handlers), before)

    def test_root_name_is_rejected(self):
        with self.assertRaises(ValueError):
            log = Log("root", to_console=False)
            self.addCleanup(log.close)

    def test_disabled_outputs_do_not_use_last_resort(self):
        output = io.StringIO()
        with patch("sys.stderr", output):
            self.make_log(to_console=False).get_logger().error("silent")
        self.assertEqual(output.getvalue(), "")

    def test_filename_collision_preserves_existing_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("common.log.datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 9, 19, 10, 0, 0)
                first = self.make_log(to_console=False, path=temp)
                first.get_logger().info("original")
                file_path = first.file_path
                first.close()
                original = file_path.read_bytes()
                with self.assertRaises(FileExistsError):
                    Log(self.id(), to_console=False, path=temp)
                self.assertEqual(file_path.read_bytes(), original)

    def test_file_open_failure_leaves_logger_unchanged(self):
        logger = logging.getLogger(self.id())
        before = (logger.level, logger.propagate, list(logger.handlers))
        with tempfile.TemporaryDirectory() as temp:
            blocked = Path(temp) / "not_a_directory"
            blocked.write_text("existing", encoding="utf-8")
            with self.assertRaises(OSError):
                Log(self.id(), path=blocked)
        self.assertEqual(
            (logger.level, logger.propagate, logger.handlers), before
        )

    def test_context_manager_restores_logger_settings(self):
        logger = logging.getLogger(self.id())
        logger.setLevel(logging.WARNING)
        logger.propagate = True
        with Log(self.id(), to_console=False) as log:
            self.assertEqual(log.get_logger().level, logging.INFO)
            self.assertFalse(logger.propagate)
        self.assertEqual(logger.level, logging.WARNING)
        self.assertTrue(logger.propagate)
        self.assertFalse(logger.handlers)
        logger.setLevel(logging.NOTSET)

    def test_cleanup_failure_preserves_and_reports_body_exception(self):
        for operation in ("flush", "close"):
            with self.subTest(operation=operation):
                output = io.StringIO()
                log = self.make_log(to_console=False)
                handler = log.get_logger().handlers[0]
                original = ValueError("original extraction failure")
                with patch.object(
                    handler, operation, side_effect=OSError("disk full")
                ), patch("sys.stderr", output):
                    with self.assertRaises(ValueError) as caught:
                        with log:
                            raise original
                self.assertIs(caught.exception, original)
                self.assertIn("disk full", output.getvalue())
                self.assertIn(self.id(), output.getvalue())
                self.assertFalse(logging.getLogger(self.id()).handlers)

    def test_cleanup_failure_without_body_exception_is_propagated(self):
        log = self.make_log(to_console=False)
        handler = log.get_logger().handlers[0]
        failure = OSError("disk full")
        with patch.object(handler, "flush", side_effect=failure):
            with self.assertRaises(OSError) as caught:
                with log:
                    pass
        self.assertIs(caught.exception, failure)

    def test_broken_diagnostic_streams_do_not_replace_body_exception(self):
        log = self.make_log(to_console=False)
        handler = log.get_logger().handlers[0]
        broken = io.StringIO()
        broken.close()
        original = ValueError("original extraction failure")
        with patch.object(
            handler, "flush", side_effect=OSError("disk full")
        ), patch("sys.stderr", broken), patch("sys.__stderr__", broken):
            with self.assertRaises(ValueError) as caught:
                with log:
                    raise original
        self.assertIs(caught.exception, original)

    def test_cleanup_failure_uses_fallback_and_releases_file(self):
        broken = io.StringIO()
        broken.close()
        fallback = io.StringIO()
        original = ValueError("original extraction failure")
        with tempfile.TemporaryDirectory() as temp:
            log = self.make_log(to_console=False, path=temp)
            handler = log.get_logger().handlers[0]
            file_stream = handler.stream
            with patch.object(
                handler, "flush", side_effect=OSError("disk full")
            ), patch("sys.stderr", broken), patch("sys.__stderr__", fallback):
                with self.assertRaises(ValueError) as caught:
                    with log:
                        raise original
            self.assertIs(caught.exception, original)
            self.assertTrue(file_stream.closed)
            self.assertIn("disk full", fallback.getvalue())

    def test_close_failure_does_not_skip_other_handlers(self):
        with tempfile.TemporaryDirectory() as temp:
            log = self.make_log(path=temp)
            logger = log.get_logger()
            console, file_handler = logger.handlers
            file_stream = file_handler.stream
            failure = OSError("console close failed")
            try:
                with patch.object(console, "close", side_effect=failure):
                    with self.assertRaises(OSError) as caught:
                        log.close()
                self.assertIs(caught.exception, failure)
                self.assertTrue(file_stream.closed)
                self.assertFalse(logger.handlers)
            finally:
                file_handler.close()

    def test_close_preserves_first_error_and_attempts_each_operation(self):
        log = self.make_log(to_console=False)
        handler = log.get_logger().handlers[0]
        flush_error = OSError("first flush failure")
        close_error = OSError("second close failure")
        with patch.object(
            handler, "flush", side_effect=flush_error
        ) as flush, patch.object(
            handler, "close", side_effect=close_error
        ) as close:
            with self.assertRaises(OSError) as caught:
                log.close()
        self.assertIs(caught.exception, flush_error)
        flush.assert_called_once_with()
        close.assert_called_once_with()

    def test_missing_process_or_thread_metadata_keeps_log_output(self):
        cases = ((False, True), (True, False), (False, False))
        for processes, threads in cases:
            with self.subTest(processes=processes, threads=threads):
                output = io.StringIO()
                with tempfile.TemporaryDirectory() as temp:
                    with patch("sys.stderr", output), patch.multiple(
                        logging, logProcesses=processes, logThreads=threads,
                        raiseExceptions=False,
                    ):
                        with Log(self.id(), path=temp) as log:
                            file_path = log.file_path
                            log.get_logger().error("failure must be visible")
                    console = output.getvalue()
                    self.assertIn("failure must be visible", console)
                    process_pattern = r"\d+" if processes else "-"
                    thread_pattern = r"\d+" if threads else "-"
                    self.assertRegex(
                        console, " {}:{} ".format(
                            process_pattern, thread_pattern
                        )
                    )
                    self.assertEqual(
                        file_path.read_text(encoding="utf-8"), console
                    )

    def test_worker_threads_write_complete_records(self):
        with tempfile.TemporaryDirectory() as temp:
            with Log(self.id(), to_console=False, path=temp) as log:
                logger = log.get_logger()
                file_path = log.file_path

                def write(worker):
                    for index in range(100):
                        logger.info("worker=%d index=%d", worker, index)

                with ThreadPoolExecutor(max_workers=4) as workers:
                    list(workers.map(write, range(4)))

            lines = file_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 400)
            messages = {line.split("] ", 1)[1] for line in lines}
            self.assertEqual(
                messages,
                {
                    "worker={} index={}".format(worker, index)
                    for worker in range(4)
                    for index in range(100)
                },
            )

    def test_import_does_not_configure_logging_or_create_files(self):
        repository = Path(__file__).resolve().parents[2]
        script = (
            "import logging, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "root = logging.getLogger(); "
            "before = (root.level, list(root.handlers)); "
            "import common.log; "
            "root = logging.getLogger(); "
            "assert before == (root.level, root.handlers); "
            "assert 'astrolabe' not in logging.Logger.manager.loggerDict"
        )
        with tempfile.TemporaryDirectory() as temp:
            subprocess.run(
                [sys.executable, "-B", "-c", script, str(repository)],
                cwd=temp, check=True, capture_output=True, text=True,
            )
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_formatter_keeps_millisecond_precision(self):
        record = logging.LogRecord(
            "test", logging.INFO, "reader.py", 7, "ok", (), None
        )
        record.created = 1750000000.123
        record.msecs = 123
        formatted = CustomFormatter().format(record)
        self.assertIsNotNone(re.search(r"\.123[+-]\d{4} ", formatted))


if __name__ == "__main__":
    unittest.main()

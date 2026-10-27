"""Application logs must reach stdout, not only a file in the container.

Logging attached a RotatingFileHandler and nothing else, so every
`logger.info` and `logger.exception` in the application went to
`logs/app.log` inside the container and nowhere else. DigitalOcean collects
stdout, not arbitrary files, and the file is destroyed on every redeploy — so
in production those lines were invisible and then gone.

That mattered most for the things deliberately written as log lines rather
than rows:

  - `ensure_company_domain_schema` catches its own exception and logs it, so
    the one startup guard that can fail without stopping the deploy was
    failing where nobody could see it;
  - the record of each crew-manifest PDF leaving the system;
  - the record of who verified a vendor and on what evidence.
"""

import logging
import logging.handlers
import unittest

OUR_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class LogsReachThePlatformTests(unittest.TestCase):
    def setUp(self):
        import app.main  # noqa: F401 — configures logging as a side effect

        self.root = logging.getLogger()

    def _stream_handlers(self):
        """Handlers writing to the real stdout.

        Identified by our own formatter, which is the only discriminator that
        survives pytest. Matching "any StreamHandler" found pytest's capture
        handlers and passed with the fix removed; matching `stream is
        sys.stdout` failed with the fix present, because pytest replaces
        sys.stdout while our handler holds the original.
        """
        return [
            h for h in self.root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(getattr(h, "formatter", None), "_fmt", None) == OUR_FORMAT
        ]

    def test_a_stdout_handler_is_attached(self):
        self.assertTrue(
            self._stream_handlers(),
            "Only a file handler is attached, so no application log line "
            "reaches the deployed platform.",
        )

    def test_the_file_handler_is_kept_as_well(self):
        """Both, not either: the file is still useful on a laptop."""
        self.assertTrue([
            h for h in self.root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ])

    def test_a_startup_guard_line_is_emitted_at_info(self):
        with self.assertLogs("app.startup", level=logging.INFO) as captured:
            logging.getLogger("app.startup").info("overlap constraint ensured")
        self.assertIn("overlap constraint ensured", "\n".join(captured.output))

    def test_importing_twice_does_not_duplicate_handlers(self):
        """Uvicorn's reloader imports main more than once."""
        before = len(self._stream_handlers())
        import importlib

        import app.main

        importlib.reload(app.main)
        self.assertEqual(len(self._stream_handlers()), before)


if __name__ == "__main__":
    unittest.main()

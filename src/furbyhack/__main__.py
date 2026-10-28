import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from furbyhack.app import App

log = logging.getLogger("furbyhack")


def setup_logging() -> Path:
    # Tray app has no console, so logs go to a file.
    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FurbyHack"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "furbyhack.log"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    ]
    if sys.stderr:  # None when started without a console (pythonw / gui-script)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return log_file


def install_crash_logging() -> None:
    """Without a console nobody sees a traceback, so uncaught errors go to the log too."""

    def on_main(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def on_thread(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
            log.critical("Uncaught exception in %s", args.thread, exc_info=exc_info)

    sys.excepthook = on_main
    threading.excepthook = on_thread


def main() -> None:
    log_file = setup_logging()
    install_crash_logging()
    log.info("FurbyHack started, logging to %s", log_file)
    App().run()


if __name__ == "__main__":
    main()

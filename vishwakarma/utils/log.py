import logging
import os
import sys

import colorlog


AI_COLOR = "#00FFFF"   # cyan — matches Holmes
TOOLS_COLOR = "magenta"


def setup_logging(level: str | None = None) -> logging.Logger:
    log_level = level or os.environ.get("LOG_LEVEL", "INFO")

    if sys.stdout.isatty():
        # Interactive terminal — use Rich so [bold] markup renders
        try:
            from rich.logging import RichHandler
            from rich.console import Console
            logging.basicConfig(
                level=log_level,
                format="%(message)s",
                handlers=[
                    RichHandler(
                        show_level=True,
                        markup=True,
                        show_time=True,
                        show_path=False,
                        console=Console(width=None),
                    )
                ],
                force=True,
            )
        except ImportError:
            _setup_colorlog(log_level)
    else:
        # Container / non-TTY — colorlog with timestamp+level (Holmes-style in kubectl logs)
        # [bold] markup appears as literal text, same as Holmes in kubectl logs
        _setup_colorlog(log_level)

    logging.getLogger().setLevel(log_level)

    # Silence noisy libraries
    for noisy in ("httpx", "httpcore", "LiteLLM", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("vishwakarma")


def _setup_colorlog(log_level: str):
    fmt = "%(log_color)s%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    colorlog.basicConfig(format=fmt, level=log_level, datefmt=datefmt)


class EndpointFilter(logging.Filter):
    """Filter out health/readiness probe logs from uvicorn access log."""

    def __init__(self, path: str):
        self.path = path
        super().__init__()

    def filter(self, record: logging.LogRecord) -> bool:
        return self.path not in record.getMessage()


def suppress_probe_logs():
    uvicorn_logger = logging.getLogger("uvicorn.access")
    uvicorn_logger.addFilter(EndpointFilter("/healthz"))
    uvicorn_logger.addFilter(EndpointFilter("/readyz"))

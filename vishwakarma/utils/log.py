import logging
import os

import colorlog


def setup_logging(level: str | None = None) -> logging.Logger:
    log_level = level or os.environ.get("LOG_LEVEL", "INFO")
    fmt = "%(log_color)s%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    colorlog.basicConfig(format=fmt, level=log_level, datefmt=datefmt)
    logging.getLogger().setLevel(log_level)

    # Silence noisy libraries
    for noisy in ("httpx", "httpcore", "LiteLLM", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("vishwakarma")


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

import argparse
import logging
import signal
import sys
import threading
from types import FrameType

from ddcs_scraper import __version__
from ddcs_scraper.api import DDCSClient
from ddcs_scraper.config import ConfigError, Settings
from ddcs_scraper.tiktok.scraper import TikTokScraper
from ddcs_scraper.worker import EXIT_API_ERROR, Worker

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ddcs-scraper",
        description=(
            "Claim videos from a ddcs-app instance, scrape their metadata from "
            "tiktok.com and post the results back. Configured through the "
            "environment or a .env file, see .env.example."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="do a single round (one claim) and exit",
    )
    parser.add_argument(
        "--max-videos",
        type=_positive_int,
        metavar="N",
        help=(
            "exit after N videos were claimed or when the queue is empty "
            "(for a first test, e.g. 10)"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as e:
        sys.stderr.write(f"Configuration error: {e}\n")
        return EXIT_API_ERROR

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    stop = threading.Event()
    _stop_on_signals(stop)

    worker = Worker(
        api=DDCSClient(
            settings.api_url,
            settings.api_token,
            scraper_id=settings.scraper_id,
            sleep=stop.wait,
        ),
        scraper=TikTokScraper(
            rate_delay=settings.rate_delay,
            rate_jitter=settings.rate_jitter,
            caption_delay=settings.caption_delay,
        ),
        claim_limit=settings.claim_limit,
        submit_batch_size=settings.submit_batch_size,
        idle_seconds=settings.idle_seconds,
        fetch_captions=settings.fetch_captions,
        stop=stop,
    )
    logger.info("Scraper '%s' working for %s.", settings.scraper_id, settings.api_url)
    return worker.run(once=args.once, max_videos=args.max_videos)


def _stop_on_signals(stop: threading.Event) -> None:
    """First Ctrl+C / SIGTERM: finish the current video, report, exit."""

    def handle(signum: int, frame: FrameType | None) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        logger.info(
            "Stopping: submitting results and releasing the remaining videos. "
            "Press Ctrl+C again to quit at once."
        )
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        msg = "must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return number

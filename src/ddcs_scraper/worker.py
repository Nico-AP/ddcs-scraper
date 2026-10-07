"""Works on ddcs-app's scrape queue: claim videos, scrape them, report back.

The handling of blocks and failures follows the built-in scraper of ddcs-app
(``ScraperService.scrape_batch``), so that a video is treated the same
whichever scraper gets it.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ddcs_scraper.api import Claim, DDCSApiError, DDCSClient, DDCSRequestError
from ddcs_scraper.tiktok.exceptions import (
    TikTokBlockedError,
    TikTokItemUnavailableError,
    TikTokScraperError,
)
from ddcs_scraper.tiktok.scraper import TikTokScraper

logger = logging.getLogger(__name__)

ABORT_REASON_BLOCKED = "blocked"
ABORT_REASON_CONSECUTIVE_FAILURES = "consecutive_failures"

# Pause after a round was aborted; doubles with each further aborted round.
COOLDOWN_BASE_SECONDS = 60 * 60
COOLDOWN_MAX_SECONDS = 24 * 60 * 60

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_API_ERROR = 2


@dataclass
class RoundStats:
    claimed: int = 0
    scraped: int = 0
    unavailable: int = 0
    failed: int = 0
    blocked: int = 0
    released: int = 0
    captions_fetched: int = 0
    captions_failed: int = 0
    # Results the server did not accept (status ``invalid``/``not_claimed``).
    rejected: int = 0
    # Videos a page was requested for, whatever the outcome.
    requested: int = 0
    aborted: bool = False
    abort_reason: str = ""
    seconds: float = 0.0

    @property
    def videos_per_minute(self) -> float:
        return round(self.requested / self.seconds * 60, 1) if self.seconds else 0.0

    @property
    def got_answers(self) -> bool:
        """Whether TikTok served at least one page with a definite answer."""
        return self.scraped + self.unavailable > 0


@dataclass
class _Outbox:
    """Collects results and sends them in batches."""

    api: DDCSClient
    batch_size: int
    stats: RoundStats
    _results: list[dict[str, Any]] = field(default_factory=list)

    def add(self, result: dict[str, Any]) -> None:
        self._results.append(result)
        if len(self._results) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        while self._results:
            batch = self._results[: self.batch_size]
            statuses = self.api.submit(batch)
            del self._results[: len(batch)]
            for status in statuses:
                if status.get("status") in ("invalid", "not_claimed"):
                    self.stats.rejected += 1
                    logger.warning(
                        "Server did not accept the result for video %s (%s): %s",
                        status.get("id_tiktok"),
                        status.get("status"),
                        status.get("detail"),
                    )


class Worker:
    # Requests TikTok refused (403/429) in a row before a round is aborted.
    CONSECUTIVE_BLOCKS_BEFORE_ABORT = 3
    # Videos failing in a row for another reason (no data on the page,
    # unexpected structure, network error) before a round is aborted.
    CONSECUTIVE_FAILURES_BEFORE_ABORT = 5
    # No new video is started this many seconds before the lease runs out.
    LEASE_MARGIN_SECONDS = 60

    def __init__(  # noqa: PLR0913
        self,
        api: DDCSClient,
        scraper: TikTokScraper,
        *,
        claim_limit: int = 100,
        submit_batch_size: int = 10,
        idle_seconds: float = 300.0,
        fetch_captions: bool = True,
        stop: threading.Event | None = None,
    ) -> None:
        self.api = api
        self.scraper = scraper
        self.claim_limit = claim_limit
        self.submit_batch_size = submit_batch_size
        self.idle_seconds = idle_seconds
        self.fetch_captions = fetch_captions
        # Set to make the worker finish the video it is on and wind down.
        self.stop = stop or threading.Event()

    def run(self, *, once: bool = False, max_videos: int | None = None) -> int:  # noqa: C901
        """Work on the queue until stopped.

        Args:
            once: Do a single round (one claim) and return.
            max_videos: Return once this many videos were claimed, or
                when the queue is empty.

        Returns:
            A process exit code.
        """
        consecutive_aborts = 0
        remaining = max_videos
        while not self.stop.is_set():
            limit = self.claim_limit
            if remaining is not None:
                limit = min(limit, remaining)

            try:
                stats = self.run_round(limit)
            except DDCSRequestError as e:
                logger.error("Stopping: %s", e)  # noqa: TRY400
                return EXIT_API_ERROR
            except DDCSApiError as e:
                # Results that could not be sent are lost; their videos
                # return to the queue when the lease runs out.
                logger.error("Could not talk to the server: %s", e)  # noqa: TRY400
                if once:
                    return EXIT_API_ERROR
                self._wait(self.idle_seconds, "trying the server again")
                continue

            if stats.claimed:
                self._log_round(stats)
            if stats.aborted:
                consecutive_aborts += 1
            elif stats.got_answers:
                consecutive_aborts = 0

            if remaining is not None:
                remaining -= stats.claimed
            if once or remaining == 0 or (remaining and not stats.claimed):
                return EXIT_ABORTED if stats.aborted else EXIT_OK
            if stats.aborted:
                self._wait(cooldown_seconds(consecutive_aborts), "scraping resumes")
            elif not stats.claimed:
                logger.info("Nothing to scrape at the moment.")
                self._wait(self.idle_seconds, "asking again")
        return EXIT_OK

    def run_round(self, limit: int) -> RoundStats:
        """Claim up to ``limit`` videos, scrape them and submit the results."""
        stats = RoundStats()
        started = time.monotonic()
        claim = self.api.claim(limit)
        stats.claimed = len(claim.video_ids)
        if not claim.video_ids:
            return stats
        logger.info(
            "Claimed %s videos, reserved for %.0f minutes.",
            stats.claimed,
            claim.lease_seconds / 60,
        )

        outbox = _Outbox(self.api, self.submit_batch_size, stats)
        # Videos no result was queued for yet; given back at the end.
        unreported = dict.fromkeys(claim.video_ids)
        try:
            self._scrape(claim, started, unreported, outbox, stats)
        except DDCSApiError:
            raise
        except Exception:
            # Don't sit on the lease when something unexpected ends the round.
            self._release(unreported, outbox, stats, best_effort=True)
            raise
        self._release(unreported, outbox, stats)
        stats.seconds = round(time.monotonic() - started, 1)
        return stats

    def _scrape(  # noqa: C901
        self,
        claim: Claim,
        started: float,
        unreported: dict[int, None],
        outbox: _Outbox,
        stats: RoundStats,
    ) -> None:
        deadline = started + claim.lease_seconds - self.LEASE_MARGIN_SECONDS
        consecutive_blocks = 0
        # Failures are held back until it is clear whether they are
        # individual ones (pages are still being served: reported as failed)
        # or the start of a streak (suspected block: videos released, so no
        # attempt is counted for them).
        failure_streak: list[tuple[int, TikTokScraperError]] = []

        def report(video_id: int, outcome: str, **fields: Any) -> None:  # noqa: ANN401
            del unreported[video_id]
            outbox.add({"id_tiktok": video_id, "outcome": outcome, **fields})

        def report_failures() -> None:
            for video_id, error in failure_streak:
                report(
                    video_id,
                    "failed",
                    error_type=type(error).__name__,
                    error_msg=str(error),
                )
                stats.failed += 1
            failure_streak.clear()

        for video_id in claim.video_ids:
            if self.stop.is_set():
                break
            if time.monotonic() > deadline:
                logger.warning("The lease is about to run out; ending the round.")
                break

            stats.requested += 1
            try:
                data = self.scraper.scrape_video(video_id)
            except TikTokBlockedError:
                # Says nothing about the video: it goes back to the queue.
                stats.blocked += 1
                consecutive_blocks += 1
                if consecutive_blocks >= self.CONSECUTIVE_BLOCKS_BEFORE_ABORT:
                    stats.aborted = True
                    stats.abort_reason = ABORT_REASON_BLOCKED
                    break
            except TikTokItemUnavailableError as error:
                # A definite answer about this video, so pages are served.
                consecutive_blocks = 0
                report_failures()
                report(
                    video_id,
                    "unavailable",
                    tiktok_status_code=_int_or_none(error.status_code),
                    error_msg=error.status_msg or "",
                )
                stats.unavailable += 1
            except TikTokScraperError as error:
                failure_streak.append((video_id, error))
                if len(failure_streak) >= self.CONSECUTIVE_FAILURES_BEFORE_ABORT:
                    failure_streak.clear()
                    stats.aborted = True
                    stats.abort_reason = ABORT_REASON_CONSECUTIVE_FAILURES
                    break
            else:
                consecutive_blocks = 0
                report_failures()
                result: dict[str, Any] = {"data": data}
                if self.fetch_captions:
                    result["caption"] = self._fetch_caption(video_id, data, stats)
                report(video_id, "success", **result)
                stats.scraped += 1

        # A streak too short to abort on: ordinary failures.
        report_failures()

    def _fetch_caption(
        self, video_id: int, data: dict[str, Any], stats: RoundStats
    ) -> dict[str, Any]:
        """Get the video's original-language caption in the shape the API expects.

        Never raises for a caption problem: the video data is worth
        submitting without it.
        """
        try:
            caption = self.scraper.fetch_original_caption(data)
        except TikTokScraperError as e:
            logger.warning("Could not fetch caption for video %s: %s", video_id, e)
            stats.captions_failed += 1
            return {"status": "failed"}

        if caption is None:
            return {"status": "none_available"}
        if not caption["vtt"]:
            # The server requires a non-empty file for "fetched".
            stats.captions_failed += 1
            return {"status": "failed"}

        stats.captions_fetched += 1
        return {
            "status": "fetched",
            "vtt": caption["vtt"],
            "language": caption["language"][:32],
            "is_auto_generated": caption["is_auto_generated"],
        }

    @staticmethod
    def _release(
        unreported: dict[int, None],
        outbox: _Outbox,
        stats: RoundStats,
        *,
        best_effort: bool = False,
    ) -> None:
        """Send what is queued and give back the videos without a result."""
        try:
            for video_id in list(unreported):
                del unreported[video_id]
                outbox.add({"id_tiktok": video_id, "outcome": "released"})
                stats.released += 1
            outbox.flush()
        except DDCSApiError:
            if not best_effort:
                raise
            logger.warning("Could not release the remaining videos.", exc_info=True)

    def _wait(self, seconds: float, what: str) -> None:
        if seconds >= 60:  # noqa: PLR2004
            logger.info("Waiting %.0f minutes before %s.", seconds / 60, what)
        self.stop.wait(seconds)

    @staticmethod
    def _log_round(stats: RoundStats) -> None:
        logger.info(
            "Round done in %.0f s (%.1f videos/min): %s scraped, %s unavailable, "
            "%s failed, %s blocked, %s released; captions: %s fetched, %s failed; "
            "%s not accepted by the server.",
            stats.seconds,
            stats.videos_per_minute,
            stats.scraped,
            stats.unavailable,
            stats.failed,
            stats.blocked,
            stats.released,
            stats.captions_fetched,
            stats.captions_failed,
            stats.rejected,
        )
        if stats.aborted:
            logger.error(
                "Round aborted (%s): TikTok appears to be blocking, or its page "
                "structure changed.",
                stats.abort_reason,
            )


def cooldown_seconds(consecutive_aborts: int) -> float:
    """Pause after the n-th aborted round in a row: 1 h, 2 h, 4 h, ... 24 h."""
    doublings = min(max(consecutive_aborts, 1) - 1, 10)
    return min(COOLDOWN_BASE_SECONDS * 2**doublings, COOLDOWN_MAX_SECONDS)


def _int_or_none(value: Any) -> int | None:  # noqa: ANN401
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

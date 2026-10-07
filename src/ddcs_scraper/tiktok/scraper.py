import random
import time
from typing import Any, TypedDict

from ddcs_scraper.tiktok.client import TikTokClient
from ddcs_scraper.tiktok.config import RATE_LIMIT_DELAY, VIDEO_URL
from ddcs_scraper.tiktok.exceptions import TikTokBlockedError
from ddcs_scraper.tiktok.parsers import TikTokParser


class ScrapedCaption(TypedDict):
    language: str  # TikTok's language tag, e.g. "deu-DE"
    is_auto_generated: bool | None
    vtt: str  # The WebVTT file as downloaded


class TikTokScraper:
    """Scraper for extracting TikTok video metadata from tiktok.com.

    Attributes:
        client (TikTokClient): HTTP client for making requests to TikTok
        parser (TikTokParser): Parser class for extracting data from responses
        rate_delay (float): Minimum time in seconds between the starts of
            two page requests (see ``_pace``)
        rate_jitter (float): Fraction by which the time between two page
            requests varies randomly around ``rate_delay`` (0.3 = ±30%)
        caption_delay (float): Pause in seconds before a caption download
    """

    def __init__(
        self,
        rate_delay: float = RATE_LIMIT_DELAY,
        client: TikTokClient | None = None,
        caption_delay: float = 0.0,
        rate_jitter: float = 0.0,
    ) -> None:
        self.client = client or TikTokClient()
        self.parser = TikTokParser
        self.rate_delay = rate_delay
        self.caption_delay = caption_delay
        self.rate_jitter = rate_jitter
        # time.monotonic() value before which no further page is requested.
        self._next_page_request_at = 0.0

    def scrape_video(self, video_id: int | str) -> dict[str, Any]:
        """Scrape a single video, paced; if blocked, retry once with new cookies.

        Args:
            video_id: ID of the video to scrape.

        Returns:
            The video's ``itemStruct`` object as found on the page.

        Raises:
            TikTokBlockedError: TikTok refused the request, also with a
                fresh session.
            TikTokClientGetError: Raised when GET request to TikTok did not
                return status ok.
            TikTokItemUnavailableError: TikTok reports the video as gone or
                private.
            TikTokMissingRehydrationDataError: When rehydration data script
                not found in response text.
            TikTokRehydrationDataAttributeError: When rehydration data is
                empty or not valid JSON.
            TikTokDataExtractionError: When TikTok hydration data structure is
                unexpected or invalid.
        """
        self._pace()
        try:
            return self._load_video(video_id)
        except TikTokBlockedError:
            self.client.reset_session()
            self._pace()
            return self._load_video(video_id)

    def fetch_original_caption(
        self, video_data: dict[str, Any]
    ) -> ScrapedCaption | None:
        """Download the original-language caption of a scraped video.

        The caption is a separate file that the video data only links to.
        The link is signed and expires within days, so this has to happen
        right after the video was scraped. Costs one extra request. It goes
        to a different host (a CDN) than the pages, so it is not subject to
        the page rate limit; ``caption_delay`` adds a pause of its own.

        Args:
            video_data: Video data as returned by ``scrape_video``.

        Returns:
            The caption, or None if the video has no original-language caption
            (no request is made in that case).

        Raises:
            TikTokClientGetError: Raised when the caption could not be
                downloaded.
        """
        caption = self.parser.select_original_caption(video_data)
        if caption is None:
            return None

        if self.caption_delay:
            time.sleep(self.caption_delay)
        response = self.client.get(caption["url"])
        return ScrapedCaption(
            language=caption.get("language") or "",
            is_auto_generated=caption.get("isAutoGen"),
            vtt=response.text,
        )

    def _load_video(self, video_id: int | str) -> dict[str, Any]:
        response = self.client.get(self.get_video_url(video_id))
        rehydration_data = self.parser.load_rehydration_data(response.text)
        return self.parser.extract_video_data(rehydration_data)

    def _pace(self) -> None:
        """Wait until the next page request is due.

        Page requests start ``rate_delay`` seconds apart on average.
        Whatever happened since the previous one started (loading the page,
        fetching a caption, the caller submitting results) counts towards
        that interval, so the wait is only what is left of it.

        With ``rate_jitter`` each interval is stretched or shortened by a
        random factor, so requests don't arrive in a perfectly regular
        rhythm. The average rate stays the same.
        """
        now = time.monotonic()
        wait = self._next_page_request_at - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        interval = self.rate_delay
        if self.rate_jitter:
            interval *= random.uniform(1 - self.rate_jitter, 1 + self.rate_jitter)  # noqa: S311
        self._next_page_request_at = now + interval

    @staticmethod
    def get_video_url(video_id: int | str) -> str:
        """Construct the generic url pointing to a video."""
        return VIDEO_URL.replace("{id}", str(video_id))

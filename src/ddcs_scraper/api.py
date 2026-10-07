"""Client for the external-scraper endpoints of ddcs-app.

See "External scrapers" in ddcs-app's docs/8_scraper.md for what the
endpoints expect and return.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

import requests

from ddcs_scraper import __version__

logger = logging.getLogger(__name__)

CLAIM_PATH = "/metadata/api/v1/tiktok/scrape-targets/claim/"
RESULTS_PATH = "/metadata/api/v1/tiktok/scrape-targets/results/"

# (connect, read) timeouts in seconds. Storing a batch of results takes the
# server a moment.
REQUEST_TIMEOUT = (10, 120)
# Waits before the 2nd, 3rd, ... attempt of a request that failed on the
# network or on the server's side.
RETRY_WAITS = (2, 5, 15, 30, 60)

_AUTH_STATUSES = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})


class DDCSApiError(Exception):
    """The server could not be reached or failed; trying again later may work."""


class DDCSRequestError(DDCSApiError):
    """The server rejected the request; trying again will not help."""


class DDCSAuthError(DDCSRequestError):
    """The token is not accepted or lacks the scraper permission."""


@dataclass(frozen=True)
class Claim:
    video_ids: list[int]
    # How long the videos stay reserved, counted from when the server
    # answered.
    lease_seconds: float


class DDCSClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        scraper_id: str = "",
        session: requests.Session | None = None,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.scraper_id = scraper_id
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": f"ddcs-scraper/{__version__}",
            }
        )
        self._sleep = sleep

    def claim(self, limit: int) -> Claim:
        """Reserve up to ``limit`` videos for scraping, in queue order."""
        response = self._post(
            CLAIM_PATH, {"limit": limit, "scraper_id": self.scraper_id}
        )
        try:
            body = response.json()
            video_ids = [int(target["id_tiktok"]) for target in body["results"]]
            lease_expires_at = datetime.fromisoformat(body["lease_expires_at"])
        except (ValueError, KeyError, TypeError) as e:
            msg = f"Unexpected answer to the claim request: {e!r}"
            raise DDCSApiError(msg) from e
        lease = lease_expires_at - self._server_time(response)
        return Claim(video_ids=video_ids, lease_seconds=lease.total_seconds())

    def submit(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Send results; returns what became of each, in the same order.

        A request the web server finds too large is split and sent again.
        """
        if not results:
            return []
        try:
            response = self._post(RESULTS_PATH, {"results": results})
        except _TooLargeError:
            if len(results) == 1:
                msg = (
                    f"The result for video {results[0].get('id_tiktok')} is "
                    f"larger than the server accepts."
                )
                raise DDCSRequestError(msg) from None
            half = len(results) // 2
            return self.submit(results[:half]) + self.submit(results[half:])
        try:
            statuses = response.json()["results"]
        except (ValueError, KeyError, TypeError) as e:
            msg = f"Unexpected answer to the results request: {e!r}"
            raise DDCSApiError(msg) from e
        if not isinstance(statuses, list) or len(statuses) != len(results):
            msg = "The server did not answer with one status per result."
            raise DDCSApiError(msg)
        return statuses

    def _post(self, path: str, payload: dict[str, Any]) -> requests.Response:
        url = self.api_url + path
        for wait in (*RETRY_WAITS, None):
            try:
                response = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                problem = f"Request to {url} failed: {e}"
            else:
                if response.ok:
                    return response
                self._raise_if_final(response, url)
                problem = f"{url} answered with status {response.status_code}"

            if wait is None:
                raise DDCSApiError(problem)
            logger.warning("%s. Trying again in %s seconds.", problem, wait)
            self._sleep(wait)
        raise AssertionError  # pragma: no cover

    @staticmethod
    def _raise_if_final(response: requests.Response, url: str) -> None:
        """Raise for answers that sending the same request again won't change."""
        status = response.status_code
        if status in _AUTH_STATUSES:
            msg = (
                f"{url} answered with status {status}: the token is not "
                f"accepted or its account lacks the permission 'Can claim "
                f"scrape targets and submit scraped results'."
            )
            raise DDCSAuthError(msg)
        if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            raise _TooLargeError
        if (
            HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            and status != HTTPStatus.TOO_MANY_REQUESTS
        ):
            msg = f"{url} answered with status {status}: {response.text[:500]}"
            raise DDCSRequestError(msg)

    @staticmethod
    def _server_time(response: requests.Response) -> datetime:
        """The server's clock, so a wrong local clock doesn't skew the lease."""
        try:
            return parsedate_to_datetime(response.headers["Date"])
        except (KeyError, ValueError, TypeError):
            return datetime.now(UTC)


class _TooLargeError(Exception):
    """The web server refused the request body as too large (HTTP 413)."""

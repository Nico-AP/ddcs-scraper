import pytest

from ddcs_scraper.api import Claim, DDCSApiError, DDCSAuthError
from ddcs_scraper.tiktok.exceptions import (
    TikTokBlockedError,
    TikTokClientGetError,
    TikTokDataExtractionError,
    TikTokItemUnavailableError,
)
from ddcs_scraper.worker import (
    EXIT_ABORTED,
    EXIT_API_ERROR,
    EXIT_OK,
    Worker,
    cooldown_seconds,
)


class FakeApi:
    def __init__(self, *claims, lease_seconds=3600.0, submit_error=None):
        self.claims = list(claims)
        self.lease_seconds = lease_seconds
        self.submit_error = submit_error
        self.limits = []
        self.requests = []

    def claim(self, limit):
        self.limits.append(limit)
        ids = self.claims.pop(0) if self.claims else []
        if isinstance(ids, Exception):
            raise ids
        return Claim(video_ids=ids[:limit], lease_seconds=self.lease_seconds)

    def submit(self, results):
        if self.submit_error:
            raise self.submit_error
        self.requests.append(results)
        return [
            {"id_tiktok": r["id_tiktok"], "status": "stored", "detail": ""}
            for r in results
        ]

    @property
    def submitted(self):
        return {r["id_tiktok"]: r for request in self.requests for r in request}


class FakeScraper:
    """Answers per video ID: a dict (the itemStruct) or an exception to raise."""

    def __init__(self, pages=None, caption=None):
        self.pages = pages or {}
        self.caption = caption
        self.requested = []

    def scrape_video(self, video_id):
        self.requested.append(video_id)
        page = self.pages.get(video_id, {"id": str(video_id)})
        if isinstance(page, Exception):
            raise page
        return page

    def fetch_original_caption(self, data):
        if isinstance(self.caption, Exception):
            raise self.caption
        return self.caption


def make_worker(api, scraper, **kwargs):
    kwargs.setdefault("idle_seconds", 0)
    return Worker(api, scraper, **kwargs)


def outcomes(api):
    return {video_id: r["outcome"] for video_id, r in api.submitted.items()}


def test_success_is_submitted_with_raw_data_and_caption():
    api = FakeApi([1])
    caption = {"language": "deu-DE", "is_auto_generated": False, "vtt": "WEBVTT\n"}
    worker = make_worker(api, FakeScraper(caption=caption))

    stats = worker.run_round(10)

    assert api.submitted[1] == {
        "id_tiktok": 1,
        "outcome": "success",
        "data": {"id": "1"},
        "caption": {
            "status": "fetched",
            "vtt": "WEBVTT\n",
            "language": "deu-DE",
            "is_auto_generated": False,
        },
    }
    assert (stats.scraped, stats.captions_fetched, stats.released) == (1, 1, 0)


def test_caption_states():
    api = FakeApi([1])
    make_worker(api, FakeScraper()).run_round(10)
    assert api.submitted[1]["caption"] == {"status": "none_available"}

    api = FakeApi([1])
    scraper = FakeScraper(caption=TikTokClientGetError("cdn"))
    stats = make_worker(api, scraper).run_round(10)
    assert api.submitted[1]["outcome"] == "success"
    assert api.submitted[1]["caption"] == {"status": "failed"}
    assert stats.captions_failed == 1

    api = FakeApi([1])
    make_worker(api, FakeScraper(), fetch_captions=False).run_round(10)
    assert "caption" not in api.submitted[1]


def test_unavailable_reports_tiktok_status_code():
    api = FakeApi([1])
    scraper = FakeScraper({1: TikTokItemUnavailableError(10204, "item doesn't exist")})

    make_worker(api, scraper).run_round(10)

    assert api.submitted[1] == {
        "id_tiktok": 1,
        "outcome": "unavailable",
        "tiktok_status_code": 10204,
        "error_msg": "item doesn't exist",
    }


def test_single_failure_is_reported_as_failed():
    api = FakeApi([1, 2])
    scraper = FakeScraper({1: TikTokDataExtractionError("no key")})

    stats = make_worker(api, scraper).run_round(10)

    assert api.submitted[1] == {
        "id_tiktok": 1,
        "outcome": "failed",
        "error_type": "TikTokDataExtractionError",
        "error_msg": "no key",
    }
    assert outcomes(api)[2] == "success"
    assert not stats.aborted


def test_failure_streak_aborts_and_releases_instead_of_failing():
    ids = list(range(1, 9))
    api = FakeApi(ids)
    scraper = FakeScraper({i: TikTokDataExtractionError("x") for i in ids})

    stats = make_worker(api, scraper).run_round(10)

    assert stats.aborted
    assert stats.abort_reason == "consecutive_failures"
    assert scraper.requested == [1, 2, 3, 4, 5]
    assert set(outcomes(api).values()) == {"released"}
    assert len(outcomes(api)) == 8
    assert stats.failed == 0


def test_blocks_abort_and_everything_unscraped_is_released():
    api = FakeApi([1, 2, 3, 4, 5])
    scraper = FakeScraper({i: TikTokBlockedError("403") for i in (2, 3, 4)})

    stats = make_worker(api, scraper).run_round(10)

    assert stats.aborted
    assert stats.abort_reason == "blocked"
    assert scraper.requested == [1, 2, 3, 4]
    assert outcomes(api) == {
        1: "success",
        2: "released",
        3: "released",
        4: "released",
        5: "released",
    }


def test_block_streak_is_reset_by_a_served_page():
    api = FakeApi([1, 2, 3, 4, 5])
    scraper = FakeScraper({i: TikTokBlockedError("403") for i in (1, 2, 4, 5)})

    stats = make_worker(api, scraper).run_round(10)

    assert not stats.aborted
    assert outcomes(api)[3] == "success"
    assert stats.released == 4


def test_results_are_sent_in_batches():
    api = FakeApi(list(range(1, 8)))

    make_worker(api, FakeScraper(), submit_batch_size=3).run_round(10)

    assert [len(request) for request in api.requests] == [3, 3, 1]


def test_expiring_lease_ends_the_round():
    api = FakeApi([1, 2], lease_seconds=30)
    scraper = FakeScraper()

    stats = make_worker(api, scraper).run_round(10)

    assert scraper.requested == []
    assert outcomes(api) == {1: "released", 2: "released"}
    assert stats.released == 2


def test_stop_releases_the_rest():
    api = FakeApi([1, 2, 3])
    worker = make_worker(api, FakeScraper())
    original = worker.scraper.scrape_video

    def scrape_then_stop(video_id):
        worker.stop.set()
        return original(video_id)

    worker.scraper.scrape_video = scrape_then_stop

    assert worker.run() == EXIT_OK
    assert outcomes(api) == {1: "success", 2: "released", 3: "released"}


def test_unexpected_error_releases_what_is_left():
    api = FakeApi([1, 2])
    scraper = FakeScraper({1: RuntimeError("bug")})

    with pytest.raises(RuntimeError):
        make_worker(api, scraper).run_round(10)

    assert outcomes(api) == {1: "released", 2: "released"}


def test_run_max_videos_limits_claims():
    api = FakeApi([1, 2, 3], [4, 5, 6])
    worker = make_worker(api, FakeScraper(), claim_limit=3)

    assert worker.run(max_videos=5) == EXIT_OK
    assert api.limits == [3, 2]
    assert len(api.submitted) == 5


def test_run_max_videos_ends_on_empty_queue():
    api = FakeApi([1])
    assert make_worker(api, FakeScraper()).run(max_videos=5) == EXIT_OK
    assert api.limits == [5, 4]


def test_run_once_reports_abort():
    api = FakeApi([1, 2, 3])
    scraper = FakeScraper({i: TikTokBlockedError("403") for i in (1, 2, 3)})

    assert make_worker(api, scraper).run(once=True) == EXIT_ABORTED


def test_run_stops_on_auth_error():
    api = FakeApi(DDCSAuthError("403"))
    assert make_worker(api, FakeScraper()).run() == EXIT_API_ERROR


def test_run_keeps_going_after_server_trouble():
    api = FakeApi(DDCSApiError("down"), [1])
    worker = make_worker(api, FakeScraper())

    assert worker.run(max_videos=1) == EXIT_OK
    assert outcomes(api) == {1: "success"}


def test_cooldown_doubles_up_to_a_day():
    hours = [cooldown_seconds(n) / 3600 for n in range(1, 8)]
    assert hours == [1, 2, 4, 8, 16, 24, 24]

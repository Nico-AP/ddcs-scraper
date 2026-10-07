from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from ddcs_scraper.api import (
    CLAIM_PATH,
    RESULTS_PATH,
    RETRY_WAITS,
    DDCSApiError,
    DDCSAuthError,
    DDCSClient,
    DDCSRequestError,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


class FakeSession:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.headers = {}
        self.posts = []

    def post(self, url, json, timeout):
        self.posts.append((url, json))
        answer = self.answers.pop(0)
        if callable(answer):
            answer = answer(json)
        if isinstance(answer, Exception):
            raise answer
        return answer


def make_client(*answers):
    session = FakeSession(*answers)
    waits = []
    client = DDCSClient(
        "https://ddcs.test/", "secret", "host-1", session=session, sleep=waits.append
    )
    return client, session, waits


def statuses(payload):
    return FakeResponse(
        body={
            "results": [
                {"id_tiktok": r["id_tiktok"], "status": "stored", "detail": ""}
                for r in payload["results"]
            ]
        }
    )


def test_claim_sends_token_and_reads_lease_from_server_clock():
    server_now = datetime(2026, 10, 7, 8, 15, tzinfo=UTC)
    client, session, _ = make_client(
        FakeResponse(
            body={
                "lease_expires_at": "2026-10-07T09:15:00Z",
                "results": [
                    {
                        "id_tiktok": 7470493179767344430,
                        "occurrence_count": 21,
                        "last_watched_at": None,
                    }
                ],
            },
            headers={"Date": format_datetime(server_now, usegmt=True)},
        )
    )

    claim = client.claim(100)

    assert session.headers["Authorization"] == "Token secret"
    assert session.posts == [
        ("https://ddcs.test" + CLAIM_PATH, {"limit": 100, "scraper_id": "host-1"})
    ]
    assert claim.video_ids == [7470493179767344430]
    assert claim.lease_seconds == timedelta(hours=1).total_seconds()


def test_transient_errors_are_retried():
    client, session, waits = make_client(
        requests.ConnectionError("down"), FakeResponse(502), statuses
    )

    result = client.submit([{"id_tiktok": 1, "outcome": "released"}])

    assert [r["status"] for r in result] == ["stored"]
    assert session.posts[0][0] == "https://ddcs.test" + RESULTS_PATH
    assert waits == list(RETRY_WAITS[:2])


def test_gives_up_after_all_retries():
    client, _, waits = make_client(*[FakeResponse(503)] * (len(RETRY_WAITS) + 1))

    with pytest.raises(DDCSApiError):
        client.claim(1)
    assert len(waits) == len(RETRY_WAITS)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, DDCSAuthError), (403, DDCSAuthError), (400, DDCSRequestError)],
)
def test_rejections_are_not_retried(status, error):
    client, _, waits = make_client(FakeResponse(status))

    with pytest.raises(error):
        client.claim(1)
    assert waits == []


def test_too_large_request_is_split():
    client, session, _ = make_client(FakeResponse(413), statuses, statuses)
    results = [{"id_tiktok": i, "outcome": "released"} for i in (1, 2, 3)]

    answer = client.submit(results)

    assert [r["id_tiktok"] for r in answer] == [1, 2, 3]
    assert [len(payload["results"]) for _, payload in session.posts] == [3, 1, 2]

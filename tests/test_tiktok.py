import json

import pytest

from ddcs_scraper.tiktok.exceptions import (
    TikTokBlockedError,
    TikTokDataExtractionError,
    TikTokItemUnavailableError,
    TikTokMissingRehydrationDataError,
    TikTokRehydrationDataAttributeError,
)
from ddcs_scraper.tiktok.parsers import TikTokParser
from ddcs_scraper.tiktok.scraper import TikTokScraper


def page(scope):
    data = json.dumps({"__DEFAULT_SCOPE__": scope})
    return (
        "<html><body>"
        '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
        f"{data}</script></body></html>"
    )


def video_page(item):
    return page({"webapp.video-detail": {"itemInfo": {"itemStruct": item}}})


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []
        self.resets = 0

    def get(self, url):
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer)

    def reset_session(self):
        self.resets += 1


def test_video_data_is_returned_as_found_on_the_page():
    item = {"id": "42", "desc": "hi", "video": {"playAddr": "https://v.test/x"}}
    scraper = TikTokScraper(rate_delay=0, client=FakeClient(video_page(item)))

    assert scraper.scrape_video(42) == item
    assert scraper.client.urls == ["https://www.tiktok.com/@tiktok/video/42"]


def test_unavailable_video_raises_with_status_code():
    text = page({"webapp.video-detail": {"statusCode": 10204, "statusMsg": "gone"}})
    scraper = TikTokScraper(rate_delay=0, client=FakeClient(text))

    with pytest.raises(TikTokItemUnavailableError) as raised:
        scraper.scrape_video(42)
    assert raised.value.status_code == 10204
    assert raised.value.status_msg == "gone"


def test_unexpected_pages_raise():
    with pytest.raises(TikTokMissingRehydrationDataError):
        TikTokParser.load_rehydration_data("<html></html>")
    with pytest.raises(TikTokRehydrationDataAttributeError):
        TikTokParser.load_rehydration_data(
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{</script>'
        )
    with pytest.raises(TikTokDataExtractionError):
        TikTokParser.extract_video_data({"__DEFAULT_SCOPE__": {}})


def test_block_is_retried_once_with_a_fresh_session():
    client = FakeClient(TikTokBlockedError("403"), video_page({"id": "1"}))
    scraper = TikTokScraper(rate_delay=0, client=client)

    assert scraper.scrape_video(1) == {"id": "1"}
    assert client.resets == 1

    client = FakeClient(TikTokBlockedError("403"), TikTokBlockedError("403"))
    with pytest.raises(TikTokBlockedError):
        TikTokScraper(rate_delay=0, client=client).scrape_video(1)


def test_original_webvtt_caption_is_fetched():
    captions = [
        {
            "isOriginalCaption": False,
            "captionFormat": "webvtt",
            "url": "https://cdn.test/en",
            "language": "eng-US",
            "isAutoGen": True,
        },
        {
            "isOriginalCaption": True,
            "captionFormat": "webvtt",
            "url": "https://cdn.test/de",
            "language": "deu-DE",
            "isAutoGen": False,
        },
    ]
    data = {"video": {"claInfo": {"captionInfos": captions}}}
    client = FakeClient("WEBVTT\n\n00:00.000 --> 00:01.000\nHallo")
    scraper = TikTokScraper(rate_delay=0, client=client)

    caption = scraper.fetch_original_caption(data)

    assert client.urls == ["https://cdn.test/de"]
    assert caption == {
        "language": "deu-DE",
        "is_auto_generated": False,
        "vtt": "WEBVTT\n\n00:00.000 --> 00:01.000\nHallo",
    }


def test_no_original_caption_means_no_request():
    client = FakeClient()
    scraper = TikTokScraper(rate_delay=0, client=client)

    assert scraper.fetch_original_caption({"video": {}}) is None
    assert scraper.fetch_original_caption({}) is None
    assert client.urls == []

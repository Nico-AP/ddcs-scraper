import json
import re
from typing import Any

from ddcs_scraper.tiktok.exceptions import (
    TikTokDataExtractionError,
    TikTokItemUnavailableError,
    TikTokMissingRehydrationDataError,
    TikTokRehydrationDataAttributeError,
)

_REHYDRATION_SCRIPT_RE = re.compile(
    r'<script[^>]*\bid="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

_VIDEO_DATA_PATH = (
    "__DEFAULT_SCOPE__",
    "webapp.video-detail",
    "itemInfo",
    "itemStruct",
)


class TikTokParser:
    """Handles parsing of HTML/JSON data received from TikTok."""

    @staticmethod
    def load_rehydration_data(response_text: str) -> dict:
        """Extract the rehydration data from the response text.

        Args:
            response_text: Content of a requests.Response object.

        Returns:
            The extracted rehydration data as a dictionary.

        Raises:
            TikTokMissingRehydrationDataError: When rehydration data script
                not found in response text.
            TikTokRehydrationDataAttributeError: When rehydration data script
                is empty or does not contain valid JSON.
        """
        match = _REHYDRATION_SCRIPT_RE.search(response_text)
        if not match:
            msg = "Rehydration data script not found in response."
            raise TikTokMissingRehydrationDataError(msg)

        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            msg = f"Rehydration data script does not contain valid JSON: {e}"
            raise TikTokRehydrationDataAttributeError(msg) from e

    @staticmethod
    def extract_video_data(json_data: dict) -> dict[str, Any]:
        """Extract the video data from the JSON response.

        Returns all the data found under the path:
        `__DEFAULT_SCOPE__.webapp.video-detail.itemInfo.itemStruct`

        Args:
            json_data: JSON response from TikTok.

        Returns:
            The extracted video data as a dictionary.

        Raises:
            TikTokItemUnavailableError: When TikTok reports a non-zero
                statusCode instead of the video data.
            TikTokDataExtractionError: When json_data does not contain expected keys.
        """
        for key in _VIDEO_DATA_PATH:
            if isinstance(json_data, dict) and key in json_data:
                json_data = json_data[key]
                continue

            # Where the data is missing, TikTok leaves a status in its place.
            status_code = (
                json_data.get("statusCode") if isinstance(json_data, dict) else None
            )
            if status_code:
                raise TikTokItemUnavailableError(
                    status_code, json_data.get("statusMsg")
                )
            msg = f"Could not find key '{key}' in json_data"
            raise TikTokDataExtractionError(msg)

        if not isinstance(json_data, dict):
            msg = "Video data is not an object"
            raise TikTokDataExtractionError(msg)
        return json_data

    @staticmethod
    def select_original_caption(video_data: dict[str, Any]) -> dict[str, Any] | None:
        """Pick the original-language caption from scraped video data.

        TikTok lists a video's captions under ``video.claInfo.captionInfos``.
        Besides the transcript in the language spoken in the video there
        may be machine translations; only the entry flagged
        ``isOriginalCaption`` is of interest. Captions in a format other
        than WebVTT are ignored.

        Args:
            video_data: Video data as returned by ``extract_video_data``.

        Returns:
            The caption entry (with ``url``, ``language``, ``isAutoGen``, ...)
            or None if the video has no original-language WebVTT caption.
        """
        file_data = video_data.get("video") or {}
        caption_infos = (file_data.get("claInfo") or {}).get("captionInfos") or []
        for caption in caption_infos:
            if (
                isinstance(caption, dict)
                and caption.get("isOriginalCaption")
                and caption.get("captionFormat") == "webvtt"
                and caption.get("url")
            ):
                return caption
        return None

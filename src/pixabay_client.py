"""Pixabay image search (free stock photos).

Search API: https://pixabay.com/api/
Params: key, q, image_type=photo, orientation=horizontal, safesearch=true, per_page=10

The API key stays server-side, so the images must be hotlinked from Pixabay's
CDN (see the README note about self-hosting later).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Final, Sequence

import requests

log = logging.getLogger(__name__)

PIXABAY_URL: Final[str] = "https://pixabay.com/api/"
PER_PAGE: Final[int] = 10


class PixabayError(RuntimeError):
    """Raised when Pixabay cannot give us usable images."""


@dataclass(frozen=True)
class ImageResult:
    """One usable image from Pixabay."""

    image_id: int
    url: str  # image file URL (webformatURL preferred, largeImageURL as backup)
    page_url: str  # human page on pixabay.com (good for the figcaption source)
    width: int
    height: int
    tags: list[str] = field(default_factory=list)
    user: str = ""

    @property
    def alt_text(self) -> str:
        """Short, descriptive alt text built from Pixabay tags."""
        if self.tags:
            words = ", ".join(self.tags[:6])
            return f"Photo of {words}"[:150]
        return "Photo from Pixabay"


class PixabayClient:
    """Small wrapper around the Pixabay search API with retries."""

    def __init__(
        self,
        api_key: str,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
    ) -> None:
        if not api_key:
            raise PixabayError("PIXABAY_API_KEY is empty. Add it to your .env file.")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._session = requests.Session()

    # -- HTTP ------------------------------------------------------------- #

    def search(self, query: str, per_page: int = PER_PAGE) -> list[dict[str, Any]]:
        """Return raw Pixabay hits for one query (empty list if no results)."""
        params = {
            "key": self._api_key,
            "q": query,
            "image_type": "photo",
            "orientation": "horizontal",
            "safesearch": "true",
            "per_page": max(1, min(per_page, 200)),
        }

        last_error = "unknown error"
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.get(
                    PIXABAY_URL, params=params, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                log.warning(
                    "Pixabay request failed (%s). Attempt %s/%s.",
                    last_error,
                    attempt,
                    self._max_attempts,
                )
                time.sleep(min(2.0 * (2 ** (attempt - 1)), 30.0))
                continue

            if response.status_code == 400:
                raise PixabayError(
                    "Pixabay rejected the request (usually a bad API key): "
                    f"{response.text[:200]}"
                )
            if response.status_code == 429:
                log.warning(
                    "Pixabay rate limit or daily limit reached "
                    "(attempt %s/%s).",
                    attempt,
                    self._max_attempts,
                )
                last_error = "HTTP 429 - rate or daily limit reached"
                time.sleep(min(5.0 * (2 ** (attempt - 1)), 60.0))
                continue
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                log.warning("Pixabay server error (%s).", last_error)
                time.sleep(min(2.0 * (2 ** (attempt - 1)), 30.0))
                continue
            if response.status_code != 200:
                raise PixabayError(
                    f"Pixabay returned unexpected HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                last_error = f"invalid JSON ({exc})"
                log.warning("Pixabay returned invalid JSON. Attempt %s/%s.", attempt, self._max_attempts)
                time.sleep(min(2.0 * (2 ** (attempt - 1)), 30.0))
                continue

            hits = payload.get("hits") or []
            log.debug("Pixabay query %r returned %s hits.", query, len(hits))
            return hits

        raise PixabayError(
            f"Pixabay failed after {self._max_attempts} attempts ({last_error})."
        )

    # -- Higher level ------------------------------------------------------ #

    def find_images(
        self,
        keywords: Sequence[str],
        fallback_query: str,
        count: int = 3,
    ) -> list[ImageResult]:
        """Collect `count` different images.

        Tries each keyword in turn. If a keyword gives nothing, it moves to the
        next one. Finally it falls back to a generic query built from the niche.
        """
        queries = [kw for kw in keywords if kw and kw.strip()]
        if fallback_query and fallback_query.strip():
            queries.append(fallback_query.strip())
        queries.extend(("technology", "workspace", "learning"))

        chosen: dict[int, ImageResult] = {}
        seen_queries: set[str] = set()

        for query in queries:
            if len(chosen) >= count:
                break
            if query.lower() in seen_queries:
                continue
            seen_queries.add(query.lower())

            try:
                hits = self.search(query)
            except PixabayError as exc:
                log.warning("Pixabay search for %r failed: %s", query, exc)
                continue

            if not hits:
                log.info("Pixabay: no results for %r, trying the next keyword.", query)
                continue

            for hit in hits:
                if len(chosen) >= count:
                    break
                image = _to_image_result(hit)
                if image is None or image.image_id in chosen:
                    continue
                chosen[image.image_id] = image

        images = list(chosen.values())
        if len(images) < count:
            log.warning(
                "Only %s of %s images found. The article will use %s image(s).",
                len(images),
                count,
                len(images),
            )
        else:
            log.info("Pixabay: collected %s different images.", len(images))
        return images


def _to_image_result(hit: dict[str, Any]) -> ImageResult | None:
    """Convert one Pixabay hit into an ImageResult (None if unusable)."""
    url = hit.get("webformatURL") or hit.get("largeImageURL") or hit.get("imageURL")
    if not url:
        return None

    raw_tags = hit.get("tags") or ""
    tags = [tag.strip() for tag in str(raw_tags).split(",") if tag.strip()]

    return ImageResult(
        image_id=int(hit.get("id") or 0),
        url=str(url),
        page_url=str(hit.get("pageURL") or "https://pixabay.com/"),
        width=int(hit.get("webformatWidth") or hit.get("imageWidth") or 0),
        height=int(hit.get("webformatHeight") or hit.get("imageHeight") or 0),
        tags=tags,
        user=str(hit.get("user") or ""),
    )
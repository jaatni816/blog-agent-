"""Blogger API v3: read the blog's history and publish one post.

We only use two methods:
  * posts.list  -> existing titles (so we never repeat a topic) and the
                   "already published today?" safety check
  * posts.insert-> publish (or save as draft) the new post
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Final, Sequence
from urllib.parse import quote

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

from src.config import IST

log = logging.getLogger(__name__)

# HTTP statuses worth retrying (rate limits and temporary server problems).
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

POST_FIELDS: Final[str] = "id,title,published,updated,status,url,selfLink,labels"
LIST_FIELDS: Final[str] = "items(id,title,published,updated,status,url)"


class BloggerError(RuntimeError):
    """Raised when the Blogger API call cannot be completed."""


@dataclass(frozen=True)
class PublishedPost:
    """What we know about a post after publishing it."""

    post_id: str
    title: str
    published: str
    is_draft: bool
    api_link: str
    blog_url: str | None
    public_url: str | None
    find_link: str | None


class BloggerClient:
    """Convenience wrapper around the two Blogger calls we need."""

    def __init__(
        self,
        service: Resource,
        blog_id: str,
        timeout_seconds: int = 60,
        max_attempts: int = 3,
    ) -> None:
        self._service = service
        self._blog_id = blog_id
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._blog_url_cache: str | None = None

    # -- plumbing ---------------------------------------------------------- #

    def _execute(self, request: Any, description: str) -> dict[str, Any]:
        """Run one API request with retries and friendly error messages."""
        last_error: str = "unknown error"

        for attempt in range(1, self._max_attempts + 1):
            try:
                result = request.execute(num_retries=0)
                log.debug("%s succeeded on attempt %s.", description, attempt)
                return result or {}
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                last_error = f"HTTP {status}: {getattr(exc, 'content', exc)}"[:300]
                if status in RETRYABLE_STATUSES and attempt < self._max_attempts:
                    delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
                    log.warning(
                        "%s failed (%s). Retrying in %.0fs (attempt %s/%s).",
                        description,
                        status,
                        delay,
                        attempt,
                        self._max_attempts,
                    )
                    time.sleep(delay)
                    continue
                if status == 403:
                    raise BloggerError(
                        f"{description} failed with 403. The token is missing the "
                        "Blogger scope, or this account cannot edit this blog."
                    ) from exc
                if status == 404:
                    raise BloggerError(
                        f"{description} failed with 404. Check BLOG_ID."
                    ) from exc
                raise BloggerError(f"{description} failed - {last_error}") from exc

        raise BloggerError(
            f"{description} failed after {self._max_attempts} attempts ({last_error})."
        )

    # -- reading ----------------------------------------------------------- #

    def list_recent_posts(self, max_results: int = 50) -> list[dict[str, Any]]:
        """Return the most recent posts (drafts included), newest first."""
        def build(with_status: bool) -> Any:
            params: dict[str, Any] = {
                "blogId": self._blog_id,
                "maxResults": max(1, min(max_results, 500)),
                "fields": LIST_FIELDS,
            }
            if with_status:
                # 'LIVE' + 'DRAFT' so a draft created today also counts as used.
                # (The Blogger API expects uppercase status values.)
                params["status"] = ["LIVE", "DRAFT"]
            return self._service.posts().list(**params)

        try:
            result = self._execute(build(True), "Reading recent posts")
        except BloggerError:
            # Older API versions dislike the status filter: try without it.
            log.debug("posts.list with status filter failed, retrying without it.")
            result = self._execute(build(False), "Reading recent posts")

        items = result.get("items") or []
        log.debug("Found %s existing post(s) on the blog.", len(items))
        return list(items)

    def list_recent_titles(self, max_results: int = 50) -> list[str]:
        """Return recent post titles, used to keep topics fresh."""
        titles = [
            str(item.get("title", "")).strip()
            for item in self.list_recent_posts(max_results=max_results)
            if str(item.get("title", "")).strip()
        ]
        log.info("Loaded %s existing post title(s) to avoid repeating topics.", len(titles))
        return titles

    def has_post_published_today(self) -> tuple[bool, str | None]:
        """Check (in IST) whether a post was already published today.

        Returns (already_published, title_of_that_post).
        """
        today = datetime.now(tz=IST).date()
        for item in self.list_recent_posts(max_results=50):
            published = str(item.get("published") or "")
            if not published:
                continue
            try:
                moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.astimezone(IST).date() == today:
                title = str(item.get("title", "<untitled>"))
                log.info("Found today's post: '%s'", title)
                return True, title
        log.info("No post published today (%s IST). Safe to publish.", today.isoformat())
        return False, None

    def get_blog_url(self) -> str | None:
        """Return the blog's public URL (for logging and post links)."""
        if self._blog_url_cache:
            return self._blog_url_cache
        try:
            result = self._execute(
                self._service.blogs().get(blogId=self._blog_id, fields="id,name,url"),
                "Reading blog details",
            )
        except BloggerError as exc:
            log.warning("Could not read the blog URL: %s", exc)
            return None
        self._blog_url_cache = result.get("url")
        return self._blog_url_cache

    # -- writing ----------------------------------------------------------- #

    def publish_post(
        self,
        title: str,
        html: str,
        labels: Sequence[str] = (),
        is_draft: bool = False,
    ) -> PublishedPost:
        """Create one post. `is_draft=True` saves it without publishing."""
        body = {
            "title": title,
            "content": html,
            "labels": list(labels),
        }
        log.info(
            "Publishing post '%s' (%s, %s labels, %s characters)...",
            title,
            "as draft" if is_draft else "LIVE",
            len(labels),
            len(html),
        )

        result = self._execute(
            self._service.posts().insert(
                blogId=self._blog_id,
                body=body,
                isDraft=bool(is_draft),
                fields=POST_FIELDS,
            ),
            "Publishing the post",
        )

        blog_url = self.get_blog_url()
        public_url = result.get("url")
        find_link = None
        if blog_url:
            # Blogger API v3 does not return the public permalink, so give the
            # user a search link that lands on the new post.
            find_link = f"{blog_url.rstrip('/')}/search?q={quote(result.get('title', title))}"

        post = PublishedPost(
            post_id=str(result.get("id", "")),
            title=str(result.get("title", title)),
            published=str(result.get("published", "")),
            is_draft=bool(is_draft),
            api_link=str(result.get("selfLink", "")),
            blog_url=blog_url,
            public_url=public_url,
            find_link=find_link,
        )
        log.info(
            "Post %s. id=%s status=%s",
            "saved as a DRAFT" if post.is_draft else "PUBLISHED",
            post.post_id,
            result.get("status", "unknown"),
        )
        if post.public_url:
            log.info("Public URL: %s", post.public_url)
        elif post.find_link:
            log.info("Find the post here: %s", post.find_link)
        return post
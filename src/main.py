"""Entry point for the Blogger Auto-Blog Agent.

Pipeline (one post per run):

    config -> Google auth -> Blogger check -> Groq topic -> Groq article
           -> Pixabay images -> compose HTML -> publish (or preview)

Usage:
    python -m src.main                # run for real (respects PUBLISH_AS_DRAFT)
    python -m src.main --dry-run      # generate everything, publish nothing
    python -m src.main --force        # skip today's "already published" check
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Final, Sequence

from src.auth import AuthError, get_blogger_service, verify_access
from src.blogger_client import BloggerClient, BloggerError
from src.composer import build_preview_document, compose_article
from src.config import IST, Config, ConfigError, OUTPUT_DIR, load_config, setup_logging
from src.groq_client import GroqClient, GroqError, count_words
from src.pixabay_client import PixabayClient, PixabayError

log = logging.getLogger("main")

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
EXIT_NO_CONFIG: Final[int] = 2
EXIT_ALREADY_PUBLISHED: Final[int] = 3

PREVIEW_FILENAME: Final[str] = "preview.html"
RUN_LOG_FILENAME: Final[str] = "last_run.json"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Publish one AI-written blog post per day on Blogger.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate the article and images, show the HTML and save "
        "output/preview.html. Never publishes anything.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the 'one post per day' check (testing only!).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


def _save_preview(article_html: str, plan, image_count: int, words: int) -> Path:
    """Write output/preview.html and return its path."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / PREVIEW_FILENAME
    path.write_text(
        build_preview_document(
            article_html, plan, image_count=image_count, word_count=words
        ),
        encoding="utf-8",
    )
    return path


def _save_run_summary(summary: dict[str, object]) -> Path:
    """Write output/last_run.json (never contains secrets)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / RUN_LOG_FILENAME
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    """Run the full pipeline and return a process exit code."""
    config: Config = load_config()
    log.info("Starting %s", "DRY RUN (nothing will be published)" if args.dry_run else "PUBLISH RUN")
    log.debug("Config: %s", config.safe_summary())

    # --- 1. Google auth ---------------------------------------------------- #
    service = get_blogger_service(config)
    verify_access(service, config.blog_id)
    blogger = BloggerClient(
        service,
        config.blog_id,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
    )

    # --- 2. Safety: one post per day (checked in IST) ---------------------- #
    already_published, existing_title = blogger.has_post_published_today()
    if already_published and not args.force:
        if args.dry_run:
            log.warning(
                "A post already exists today ('%s'), but this is a dry run so "
                "nothing will be published. Use --force to preview anyway.",
                existing_title,
            )
        else:
            log.info(
                "STOP: '%s' was already published today (%s IST). Nothing to do. "
                "Use --force only for testing.",
                existing_title,
                config.today_ist,
            )
            return EXIT_ALREADY_PUBLISHED
    elif already_published:
        log.warning("--force given: ignoring the 'already published today' check.")

    # --- 3. Topic ---------------------------------------------------------- #
    existing_titles = blogger.list_recent_titles(config.recent_titles_limit)
    groq = GroqClient(
        api_key=config.groq_api_key,
        model=config.groq_model,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
        max_tokens=config.groq_max_tokens,
        niche=config.niche,
        language=config.language,
    )
    plan = groq.choose_topic(existing_titles)

    # --- 4. Article -------------------------------------------------------- #
    article_html = groq.write_article(plan)

    # --- 5. Images --------------------------------------------------------- #
    pixabay = PixabayClient(
        api_key=config.pixabay_api_key,
        timeout_seconds=config.timeout_seconds,
        max_attempts=config.max_attempts,
    )
    images = pixabay.find_images(
        keywords=plan.image_keywords,
        fallback_query=config.niche,
        count=config.image_count,
    )

    # --- 6. Compose -------------------------------------------------------- #
    final_html = compose_article(
        article_html,
        images,
        title=plan.title,
        meta_description=plan.meta_description,
    )
    words = count_words(final_html)

    preview_path = _save_preview(final_html, plan, len(images), words)
    log.info("Preview saved to %s", preview_path)

    summary: dict[str, object] = {
        "date_ist": config.today_ist,
        "finished_ist": datetime.now(tz=IST).isoformat(timespec="seconds"),
        "model": config.groq_model,
        "title": plan.title,
        "meta_description": plan.meta_description,
        "image_keywords": plan.image_keywords,
        "labels": plan.labels,
        "images_used": len(images),
        "words": words,
        "dry_run": bool(args.dry_run),
    }

    # --- 7. Publish or preview --------------------------------------------- #
    if args.dry_run:
        print("\n" + "=" * 78)
        print("DRY RUN - final HTML (also saved to " + str(preview_path) + ")")
        print("=" * 78)
        print(final_html)
        print("=" * 78)
        print(f"Title:   {plan.title}")
        print(f"Meta:    {plan.meta_description}")
        print(f"Labels:  {', '.join(plan.labels)}")
        print(f"Images:  {len(images)} placed")
        print(f"Words:   {words}")
        print("Nothing was published. Re-run without --dry-run to publish.")
        print("=" * 78)
        _save_run_summary(summary)
        return EXIT_OK

    post = blogger.publish_post(
        title=plan.title,
        html=final_html,
        labels=plan.labels,
        is_draft=config.publish_as_draft,
    )
    summary["post_id"] = post.post_id
    summary["post_url"] = post.public_url or post.find_link
    summary["is_draft"] = post.is_draft
    _save_run_summary(summary)

    log.info("Run finished. Summary written to %s", OUTPUT_DIR / RUN_LOG_FILENAME)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)

    try:
        return run(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_NO_CONFIG
    except (AuthError, BloggerError, GroqError, PixabayError) as exc:
        log.error("Run failed: %s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
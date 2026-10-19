"""Merge the generated article with the Pixabay images.

The writer leaves three markers in the article:
    {{IMAGE_1}} {{IMAGE_2}} {{IMAGE_3}}

This module replaces each marker with a real <figure> block. Any marker that has
no matching image (or that is left over) is removed, so the final HTML is always
valid and clean.
"""

from __future__ import annotations

import logging
import re
from html import escape
from typing import Final, Sequence

from src.groq_client import IMAGE_PLACEHOLDERS, TopicPlan, count_words
from src.pixabay_client import ImageResult

log = logging.getLogger(__name__)

FIGURE_STYLE: Final[str] = "max-width:100%;height:auto"
CAPTION_TEXT: Final[str] = "Image: Pixabay"


def build_figure(image: ImageResult, index: int, context: str = "") -> str:
    """Return the <figure> block for one image.

    Alt text comes from the Pixabay tags plus a short hint about the section it
    sits in, which helps both accessibility and Google Images.
    """
    alt_parts = [image.alt_text]
    if context:
        alt_parts.append(context.strip())
    alt_text = escape(" - ".join(part for part in alt_parts if part)[:160], quote=True)
    source = escape(CAPTION_TEXT)

    return (
        f'<figure>'
        f'<img src="{escape(image.url, quote=True)}" '
        f'alt="{alt_text}" style="{FIGURE_STYLE}"/>'
        f"<figcaption>{source}</figcaption>"
        f"</figure>"
    )


def _section_context(html: str, placeholder: str) -> str:
    """Find the nearest <h2> heading above a placeholder (short version)."""
    index = html.find(placeholder)
    if index == -1:
        return ""
    heading = None
    for match in re.finditer(r"<h2[^>]*>(.*?)</h2>", html[:index], flags=re.IGNORECASE | re.DOTALL):
        heading = match.group(1)
    if not heading:
        return ""
    clean = re.sub(r"<[^>]+>", "", heading).strip()
    return escape(re.sub(r"\s+", " ", clean)[:80], quote=True)


def compose_article(
    article_html: str,
    images: Sequence[ImageResult],
    *,
    title: str = "",
    meta_description: str = "",
) -> str:
    """Insert images into the article and return the final HTML."""
    html = article_html
    used = 0

    for index, placeholder in enumerate(IMAGE_PLACEHOLDERS):
        if placeholder not in html:
            continue
        if index < len(images):
            html = html.replace(
                placeholder,
                build_figure(images[index], index + 1, _section_context(html, placeholder)),
                1,
            )
            used += 1
        else:
            # Not enough images: drop the marker, keep the text flowing.
            html = html.replace(placeholder, "", 1)
            log.info(
                "No image available for %s - removed the marker and kept the text.",
                placeholder,
            )

    # Safety net: remove any stray placeholder the writer invented.
    html = re.sub(r"\{\{IMAGE_\d+\}\}", "", html)

    # Tidy up whitespace left behind by the replacements.
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r"[ \t]+\n", "\n", html)

    words = count_words(html)
    figures = html.lower().count("<figure")
    log.info("Composed final HTML: ~%s words, %s image(s) placed.", words, figures)

    if meta_description:
        # Helpful for the preview file and harmless in the Blogger body.
        log.debug("Meta description (%s chars): %s", len(meta_description), meta_description)
    return html.strip()


def build_preview_document(
    article_html: str,
    plan: TopicPlan,
    *,
    image_count: int = 0,
    word_count: int | None = None,
) -> str:
    """Wrap the article in a full HTML page so output/preview.html is readable."""
    words = word_count if word_count is not None else count_words(article_html)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(plan.title)}</title>
  <meta name="description" content="{escape(plan.meta_description, quote=True)}">
  <meta name="robots" content="noindex, nofollow">
  <style>
    body {{ font-family: Georgia, "Times New Roman", serif; line-height: 1.7;
            max-width: 760px; margin: 40px auto; padding: 0 20px; color: #222; }}
    h2 {{ margin-top: 32px; }}
    figure {{ margin: 28px 0; }}
    img {{ border-radius: 6px; }}
    figcaption {{ font-size: 13px; color: #666; margin-top: 6px; }}
    .preview-meta {{ background: #f5f5f5; border-left: 4px solid #888;
                     padding: 12px 16px; font-family: system-ui, sans-serif; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="preview-meta">
    <strong>DRY RUN - this file was not published.</strong><br>
    Title: {escape(plan.title)}<br>
    Labels: {escape(", ".join(plan.labels))}<br>
    Images planned: {image_count} &middot; Words: {words}
  </div>
  <article>
{article_html}
  </article>
</body>
</html>
"""
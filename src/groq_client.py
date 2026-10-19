"""Groq API client: choose a fresh topic, then write the full article.

Two calls per run:
  1. `choose_topic()`   -> strict JSON plan (title, meta description, keywords, labels)
  2. `write_article()`  -> a 1000-1500 word HTML article with image placeholders

Groq is OpenAI-compatible, so we POST to
https://api.groq.com/openai/v1/chat/completions with plain `requests`.

Every HTTP call retries up to `max_attempts` times with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Final, Sequence

import requests

log = logging.getLogger(__name__)

GROQ_CHAT_URL: Final[str] = "https://api.groq.com/openai/v1/chat/completions"

# Placeholders the writer must place in the article; composer.py replaces them.
IMAGE_PLACEHOLDERS: Final[tuple[str, ...]] = ("{{IMAGE_1}}", "{{IMAGE_2}}", "{{IMAGE_3}}")

# Tags we allow in the generated article (Blogger will render them fine).
ALLOWED_TAGS: Final[str] = "h2, h3, p, ul, li, strong"

MIN_WORDS: Final[int] = 1000
MAX_WORDS: Final[int] = 1500

_TOPIC_JSON_SHAPE: Final[str] = json.dumps(
    {
        "title": "string, max 70 characters",
        "meta_description": "string, 140-160 characters",
        "image_keywords": ["keyword 1", "keyword 2", "keyword 3"],
        "labels": ["tag1", "tag2", "tag3"],
    },
    indent=2,
)


class GroqError(RuntimeError):
    """Raised when Groq cannot give us a usable answer."""


@dataclass(frozen=True)
class TopicPlan:
    """The daily content plan produced by the model."""

    title: str
    meta_description: str
    image_keywords: list[str]
    labels: list[str]


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #


def strip_code_fences(text: str) -> str:
    """Remove ``` fences a model sometimes adds around its answer."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response."""
    cleaned = strip_code_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in the model response.") from None
        data = json.loads(cleaned[start : end + 1])

    if not isinstance(data, dict):
        raise ValueError("Model response was not a JSON object.")
    return data


def strip_html_tags(html: str) -> str:
    """Return readable text from an HTML fragment."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def count_words(html_or_text: str) -> int:
    """Count real words (HTML tags and entities do not count)."""
    text = strip_html_tags(html_or_text)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text))


def sanitize_label(label: str) -> str:
    """Make a label safe for Blogger (lowercase letters, digits, dashes)."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(label).strip().lower()).strip("-")
    return cleaned[:30]


def clean_article_html(raw: str, title: str) -> str:
    """Normalise the model's HTML: no fences, no h1, no <html>/<body> wrappers."""
    html = strip_code_fences(raw)

    # Remove a full document wrapper if the model produced one.
    match = re.search(r"<body[^>]*>(.*)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    if match:
        html = match.group(1)
    else:
        html = re.sub(r"</?(html|head|body)[^>]*>", "", html, flags=re.IGNORECASE)

    # A title should live in Blogger's title field, not in the body.
    html = re.sub(r"<h1[^>]*>.*?</h1>", "", html, flags=re.IGNORECASE | re.DOTALL)

    # Drop a plain-text copy of the title if it starts the article.
    lines = html.splitlines()
    while lines and (not lines[0].strip() or lines[0].strip().lower() == title.strip().lower()):
        lines.pop(0)
    html = "\n".join(lines).strip()

    # Blank out tags we do not want, keeping their text.
    html = re.sub(r"</?(h4|h5|h6|div|span|br|img|figure|table|em|i|b)\b[^>]*>", "", html, flags=re.IGNORECASE)

    # Tidy whitespace and give every block element its own line.
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r"(</(?:p|li|h2|h3|ul)>)\s*(?=<)", r"\1\n\n", html, flags=re.IGNORECASE)
    return html.strip()


def article_problems(html: str) -> list[str]:
    """Return a list of quality problems found in a generated article."""
    problems: list[str] = []
    lowered = html.lower()

    if "<h1" in lowered:
        problems.append("contains an <h1> (the title belongs in the title field)")
    if re.search(r"<\s*(html|body|head)\b", lowered):
        problems.append("contains an <html>/<body>/<head> wrapper")
    if "```" in html:
        problems.append("contains a markdown code fence")
    if re.search(r"(?m)^#{1,6}\s+", html):
        problems.append("contains markdown headings")
    if re.search(r"\*\*[^*\n]+\*\*", html):
        problems.append("contains markdown bold syntax")

    missing = [p for p in IMAGE_PLACEHOLDERS if p not in html]
    if missing:
        problems.append(f"is missing the image placeholders {', '.join(missing)}")

    h2_count = len(re.findall(r"<h2\b", lowered))
    if h2_count < 4:
        problems.append(f"has only {h2_count} <h2> sections (need at least 4)")

    words = count_words(html)
    if words < 700:
        problems.append(f"is too short ({words} words)")
    elif words < MIN_WORDS:
        problems.append(f"is slightly short ({words} words, want {MIN_WORDS}-{MAX_WORDS})")
    elif words > MAX_WORDS * 1.3:
        problems.append(f"is too long ({words} words, want {MIN_WORDS}-{MAX_WORDS})")

    return problems


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

TOPIC_SYSTEM_PROMPT = """\
You are the editor of a personal blog. You plan one high-quality article per day.

Rules you must follow:
- The article must be inside this niche: {niche}
- Write everything in this language: {language}
- The topic must NOT repeat or closely overlap any previously published title.
- Prefer practical, beginner-friendly angles: how-to guides, step-by-step \
explainers, comparisons, common mistakes, tool round-ups.
- Be specific and honest. No clickbait, no fake news, no made-up statistics or quotes.
- Never use emojis, hashtags, or ALL CAPS in the title.

Return STRICT JSON and nothing else (no markdown, no explanation), in exactly \
this shape:
{json_shape}

Field rules:
- "title": under 70 characters, clear and specific, no trailing punctuation.
- "meta_description": 140-160 characters, plain language, mentions the main topic once.
- "image_keywords": exactly 3 simple English keywords for a stock photo search. \
Use everyday nouns (no brand names, no person names).
- "labels": 3 to 5 short lowercase tags, no spaces (use dashes if needed).
"""


ARTICLE_SYSTEM_PROMPT = """\
You are a senior blog writer who writes clear, honest, useful articles.

Topic: {title}
Niche: {niche}
Language: {language}

Output rules (very important):
- Output ONLY an HTML fragment. No ``` fences, no markdown, no explanations.
- Use ONLY these tags: {allowed_tags}. No <h1> (the title is added separately), \
no <html>, <head>, <body>, <div>, <span>, <img> or <style>.
- Structure:
  1. An engaging introduction of 2-3 short paragraphs (no heading before it).
  2. Between 4 and 6 main sections, each starting with an <h2>.
  3. Practical tips, steps or concrete examples inside the sections. Use at least \
one <ul> with <li> items and at least one <h3> sub-heading.
  4. A short conclusion section with an <h2> (2-3 sentences, no sales pitch).
  5. A final section with <h2>Frequently Asked Questions</h2> holding exactly 3 \
<h3> questions, each followed by a short <p> answer.
- Images: place the exact text {placeholders} on its own line in three different \
sections, at a spot where a photo genuinely helps.
- Length: {min_words}-{max_words} words.
- Style: warm, human, plain English. Explain jargon the first time you use it. \
Use <strong> only for the few words that really matter.
- Never invent statistics, studies, prices, dates or quotes. Never attribute a \
quote to a person or company. Never use hype or clickbait.
"""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass
class GroqClient:
    """Thin, well-behaved wrapper around the Groq chat completions API."""

    api_key: str
    model: str
    timeout_seconds: int = 60
    max_attempts: int = 3
    max_tokens: int = 8192
    niche: str = ""
    language: str = "English"
    _session: requests.Session = field(default_factory=requests.Session, repr=False)
    _supports_json_mode: bool = True

    # -- HTTP ------------------------------------------------------------- #

    def _chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """POST one chat completion and return the assistant's text.

        Retries transient failures (network, 429, 5xx) with exponential
        backoff. Bad keys and bad requests fail fast with a clear message.
        """
        if not self.api_key:
            raise GroqError("GROQ_API_KEY is empty. Add it to your .env file.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        base_payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.9,
        }

        last_error = "unknown error"
        for attempt in range(1, self.max_attempts + 1):
            payload = dict(base_payload)
            if json_mode and self._supports_json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                response = self._session.post(
                    GROQ_CHAT_URL, headers=headers, json=payload, timeout=self.timeout_seconds
                )
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
                log.warning("Groq request failed (%s). Attempt %s/%s.", last_error, attempt, self.max_attempts)
                self._sleep_before_retry(attempt)
                continue

            status = response.status_code

            # This model does not support JSON mode: turn it off and continue.
            if (
                json_mode
                and status == 400
                and self._supports_json_mode
                and "response_format" in response.text
            ):
                log.warning("Model %s has no JSON mode; falling back to plain text + parser.", self.model)
                self._supports_json_mode = False
                continue

            if status == 401 or status == 403:
                raise GroqError(
                    f"Groq rejected the API key (HTTP {status}). Check GROQ_API_KEY."
                )
            if status == 404:
                raise GroqError(
                    f"Groq does not know the model '{self.model}' (HTTP 404). "
                    "Check GROQ_MODEL, or list current models at "
                    "https://api.groq.com/openai/v1/models"
                )
            if status == 400:
                raise GroqError(f"Groq rejected the request (HTTP 400): {response.text[:400]}")

            if status == 429 or status >= 500:
                last_error = f"HTTP {status}: {response.text[:200]}"
                log.warning("Groq is busy (%s). Attempt %s/%s.", last_error, attempt, self.max_attempts)
                self._sleep_before_retry(attempt)
                continue

            if status != 200:
                raise GroqError(f"Groq returned unexpected HTTP {status}: {response.text[:400]}")

            try:
                data = response.json()
                message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError) as exc:
                raise GroqError(f"Could not read Groq's response: {exc}") from exc

            content = (message.get("content") or "").strip()
            if not content:
                reasoning = (message.get("reasoning") or "")[:200]
                raise GroqError(
                    "Groq returned an empty answer "
                    f"(model finish_reason={data['choices'][0].get('finish_reason')!r}). "
                    "Reasoning models can eat the whole token budget - raise GROQ_MAX_TOKENS."
                    + (f" Reasoning preview: {reasoning}" if reasoning else "")
                )
            return content

        raise GroqError(
            f"Groq failed after {self.max_attempts} attempts ({last_error}). Try again later."
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        """Exponential backoff: 2s, 4s, 8s ..."""
        delay = min(2.0 * (2 ** (attempt - 1)), 30.0)
        time.sleep(delay)

    # -- Step 1: topic ---------------------------------------------------- #

    def choose_topic(self, existing_titles: Sequence[str] | None = None) -> TopicPlan:
        """Ask Groq for today's topic, avoiding anything already published."""
        titles = [t.strip() for t in (existing_titles or []) if t and t.strip()]
        if titles:
            history = "\n".join(f"{i}. {title}" for i, title in enumerate(titles, start=1))
            history_block = (
                "Titles that are ALREADY PUBLISHED on this blog. Pick something clearly "
                "different from all of them:\n" + history
            )
        else:
            history_block = "This blog has no posts yet, so any topic inside the niche is fine."

        system_prompt = TOPIC_SYSTEM_PROMPT.format(
            niche=self.niche,
            language=self.language,
            json_shape=_TOPIC_JSON_SHAPE,
        )
        user_prompt = (
            f"{history_block}\n\n"
            "Choose today's article topic now. Output the JSON object only."
        )

        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            log.info("Groq: choosing a fresh topic (attempt %s/%s)...", attempt, self.max_attempts)
            raw = self._chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
                max_tokens=min(2048, self.max_tokens),
                json_mode=True,
            )
            try:
                return self._parse_topic(raw, titles)
            except ValueError as exc:
                last_problem = str(exc)
                log.warning("Topic answer was not usable: %s", last_problem)

        raise GroqError(f"Could not get a valid topic after {self.max_attempts} tries: {last_problem}")

    def _parse_topic(self, raw: str, existing_titles: Sequence[str]) -> TopicPlan:
        """Validate the JSON plan, raising ValueError with a readable reason."""
        data = extract_json_object(raw)

        title = str(data.get("title", "")).strip()
        if len(title) < 10:
            raise ValueError("title is missing or too short")
        title = re.sub(r"\s+", " ", title)[:120]

        meta = str(data.get("meta_description", "")).strip()
        if not 20 <= len(meta) <= 320:
            raise ValueError(f"meta_description has an odd length ({len(meta)} chars)")

        keywords = [
            re.sub(r"\s+", " ", str(kw)).strip()
            for kw in data.get("image_keywords", [])
            if str(kw).strip()
        ]
        if len(keywords) < 1:
            raise ValueError("image_keywords is empty")
        # Exactly three keywords is what the composer expects.
        fallbacks = ("technology", "workspace", "learning")
        while len(keywords) < 3:
            keywords.append(fallbacks[(len(keywords) - 1) % len(fallbacks)])
        keywords = keywords[:3]

        labels = [sanitize_label(lb) for lb in data.get("labels", []) if sanitize_label(lb)]
        if not labels:
            labels = [sanitize_label(w) for w in keywords[:3]]
        labels = list(dict.fromkeys(labels))[:5]

        plan = TopicPlan(
            title=title,
            meta_description=meta,
            image_keywords=keywords,
            labels=labels,
        )
        log.info(
            "Topic chosen: '%s' | keywords=%s | labels=%s",
            plan.title,
            ", ".join(plan.image_keywords),
            ", ".join(plan.labels),
        )
        if _is_near_duplicate(title, existing_titles):
            log.warning(
                "This title looks close to an older post: %r", title
            )
        return plan

    # -- Step 2: article -------------------------------------------------- #

    def write_article(self, plan: TopicPlan) -> str:
        """Ask Groq to write the full article as clean HTML."""
        system_prompt = ARTICLE_SYSTEM_PROMPT.format(
            title=plan.title,
            niche=self.niche,
            language=self.language,
            allowed_tags=ALLOWED_TAGS,
            placeholders=", ".join(IMAGE_PLACEHOLDERS),
            min_words=MIN_WORDS,
            max_words=MAX_WORDS,
        )
        user_prompt = (
            f"Title: {plan.title}\n"
            f"Meta description: {plan.meta_description}\n"
            f"Target search keywords: {plan.title}\n\n"
            "Write the complete article now, following every output rule. "
            "Start directly with the introduction paragraph."
        )

        best_html = ""
        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            log.info("Groq: writing the article (attempt %s/%s)...", attempt, self.max_attempts)
            raw = self._chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.6,
                max_tokens=self.max_tokens,
            )
            html = clean_article_html(raw, plan.title)
            html = self._ensure_placeholders(html)
            problems = article_problems(html)

            hard = [p for p in problems if "slightly short" not in p and "too long" not in p]
            if not hard:
                words = count_words(html)
                log.info("Article ready: %s words, %s sections.", words, html.lower().count("<h2"))
                if problems:
                    log.warning("Minor notes: %s", "; ".join(problems))
                return html

            best_html = best_html or html
            last_problem = "; ".join(problems)
            log.warning("Article needs a rewrite: %s", last_problem)
            if attempt < self.max_attempts:
                self._sleep_before_retry(attempt)

        if best_html:
            log.warning("Using the best article we got despite warnings: %s", last_problem)
            return best_html
        raise GroqError(f"Groq could not produce a usable article: {last_problem}")

    @staticmethod
    def _ensure_placeholders(html: str) -> str:
        """Guarantee the three image placeholders exist, even if the model forgot."""
        if all(placeholder in html for placeholder in IMAGE_PLACEHOLDERS):
            return html

        # Add any missing placeholder after the second, third and fourth h2.
        sections = html.split("</h2>")
        positions = [min(i, len(sections) - 1) for i in (2, 3, 4)]
        for placeholder, position in zip(IMAGE_PLACEHOLDERS, positions):
            if placeholder not in html:
                log.warning("Model forgot %s; inserting it automatically.", placeholder)
                sections[position] = f"{sections[position]}\n\n{placeholder}"
        return "</h2>".join(sections)


def _is_near_duplicate(title: str, existing_titles: Sequence[str], threshold: float = 0.75) -> bool:
    """Cheap duplicate check: Jaccard similarity of lowercased words."""
    def tokens(text: str) -> set[str]:
        return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 3}

    new_tokens = tokens(title)
    if not new_tokens:
        return False

    for old_title in existing_titles:
        old_tokens = tokens(old_title)
        if not old_tokens:
            continue
        overlap = len(new_tokens & old_tokens) / len(new_tokens | old_tokens)
        if overlap >= threshold:
            return True
    return False
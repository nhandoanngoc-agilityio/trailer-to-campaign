"""Social Posts, and the check that every fact in them traces back to its source.

The Copywriter submits Social Posts as tool input; trace() turns them into
SocialPost records or refuses the whole set with every problem it found. Traced means:

- one post per platform and language: Facebook, Instagram, TikTok, each in en and vi
- each within its platform's limits, naming the Film exactly as the catalog does
- a vi post is written in Vietnamese
- no control characters but a newline: junk a reader can't see, like a form feed where an emoji was meant
- every claim cites a Key Moment (key_moment:N) or a Catalog Metadata field (catalog:title)
- every quoted line is a Key Moment's quote, word for word and cited to that moment;
  in a vi post too, where the English line stays as heard and a gloss may follow unquoted

Code can check that sources exist and quotes are exact; whether a claim says only what
its source says is for the Checker and the marketer's Approval.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from trailer_to_campaign.agents import ChecksFailed
from trailer_to_campaign.agents.brand import WORD, brand_problems
from trailer_to_campaign.agents.key_moments import KeyMoment
from trailer_to_campaign.agents.post_text import HASHTAG, QUOTED, nfc, own_words, whole_words

PLATFORM_LIMITS = {"facebook": 500, "instagram": 2200, "tiktok": 150}  # characters
MAX_HASHTAGS = {"instagram": 5}
LANGUAGES = ("en", "vi")
CATALOG_FIELDS = ("title", "synopsis", "genre", "language", "runtime_minutes", "cast")
ALL_PAIRS = tuple((platform, language) for platform in PLATFORM_LIMITS for language in LANGUAGES)

OUTER_PUNCTUATION = " \t\n.,!?;:…"
# Marks found in Vietnamese and almost nowhere else: breve (ă), horn (ơ ư), hook above (ả), dot below (ạ).
VIETNAMESE_MARKS = {"̆", "̛", "̉", "̣"}


class NotTraced(ChecksFailed):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("Social Posts not traced", problems)


@dataclass(frozen=True)
class Claim:
    text: str
    source: str  # key_moment:N (1-based) or catalog:<field>


@dataclass(frozen=True)
class SocialPost:
    platform: str
    language: str
    text: str
    claims: list[Claim]


def trace(
    submitted: list[Any],
    key_moments: list[KeyMoment],
    catalog: dict[str, Any],
    required: Iterable[tuple[str, str]] = ALL_PAIRS,
) -> list[SocialPost]:
    """Return the posts in platform and language order, or raise NotTraced listing every problem.

    required is the (platform, language) posts asked for: all six for a first draft,
    only the rejected ones for a rewrite.
    """
    wanted = set(required)
    title = nfc(str(catalog["title"]))
    catalog_words = _catalog_words(catalog)
    problems: list[str] = []
    posts: list[SocialPost] = []
    for i, raw in enumerate(submitted, start=1):
        try:
            post = _parse(raw)
        except (KeyError, TypeError, AttributeError):
            problems.append(f"post {i}: needs platform, language, text and claims (each claim a text and a source)")
            continue
        problems += [
            f"post {i} ({post.platform} {post.language}): {p}"
            for p in _problems(post, title, catalog_words, key_moments)
        ]
        posts.append(post)

    for platform, language in ALL_PAIRS:
        n = sum(p.platform == platform and p.language == language for p in posts)
        if (platform, language) not in wanted:
            if n:
                asked = ", ".join(f"{p} {lang}" for p, lang in ALL_PAIRS if (p, lang) in wanted)
                problems.append(f"write only the posts asked for ({asked}), not the {platform} post in {language}")
        elif n == 0:
            problems.append(f"no {platform} post in {language}; write one")
        elif n > 1:
            problems.append(f"the {platform} post in {language} is written {'twice' if n == 2 else f'{n} times'}")
    if problems:
        raise NotTraced(problems)
    return sorted(posts, key=lambda p: ALL_PAIRS.index((p.platform, p.language)))


def _parse(raw: dict[str, Any]) -> SocialPost:
    claims = [Claim(str(c["text"]).strip(), str(c["source"]).strip()) for c in raw["claims"]]
    return SocialPost(str(raw["platform"]), str(raw["language"]), str(raw["text"]).strip(), claims)


def _catalog_words(catalog: dict[str, Any]) -> set[str]:
    """Words the catalog writes, which a post may write the same way (IMAX, NASA)."""
    parts = [str(catalog.get(f, "")) for f in ("title", "synopsis")] + [str(g) for g in catalog.get("genre", [])]
    parts += [str(c.get(k) or "") for c in catalog.get("cast", []) for k in ("character", "name")]
    return set(WORD.findall(nfc(" ".join(parts))))


def _problems(post: SocialPost, title: str, catalog_words: set[str], key_moments: list[KeyMoment]) -> list[str]:
    platform, language, claims = post.platform, post.language, post.claims
    text = nfc(post.text)  # the same letters can be one code point or two; compare them one way
    problems = []
    if platform not in PLATFORM_LIMITS:
        problems.append(f"platform {platform!r} is not one of {', '.join(PLATFORM_LIMITS)}")
    if language not in LANGUAGES:
        problems.append(f"language {language!r} is not one of {', '.join(LANGUAGES)}")
    if problems:
        return problems

    limit = PLATFORM_LIMITS[platform]
    if len(text) > limit:
        problems.append(f"{platform} posts are at most {limit} characters; this one is {len(text)}")
    problems += _control_problems(text)
    hashtags = len(HASHTAG.findall(text))
    if platform in MAX_HASHTAGS and hashtags > MAX_HASHTAGS[platform]:
        problems.append(f"{platform} posts have at most {MAX_HASHTAGS[platform]} hashtags; this one has {hashtags}")
    if not whole_words(title).search(text):
        problems.append(f"name the Film exactly as the catalog does: {title!r}")
    if language == "vi" and not _vietnamese(own_words(text, title)):
        problems.append("a vi post must be written in Vietnamese, not only its title, quotes or gloss")
    problems += brand_problems(text, language, title, catalog_words)

    if not claims:
        problems.append("list every factual claim in the post, each with its source")
    for claim in claims:
        if not _source_exists(claim.source, len(key_moments)):
            problems.append(
                f"claim {claim.text!r} cites {claim.source!r}; cite key_moment:1 to key_moment:{len(key_moments)}, "
                f"or catalog: and one of {', '.join(CATALOG_FIELDS)}"
            )
    problems += _quote_problems(text, title, claims, key_moments)
    return problems


def _control_problems(text: str) -> list[str]:
    """Control characters (Cc) other than a newline, and lone surrogates (Cs): half an emoji escaped by hand."""
    found = dict.fromkeys(c for c in text if c != "\n" and unicodedata.category(c) in ("Cc", "Cs"))
    if not found:
        return []
    codes = ", ".join(f"U+{ord(c):04X}" for c in found)
    return [
        f"remove the control character{'s' if len(found) > 1 else ''} {codes}: write an emoji as the emoji itself, "
        "not an escape code like \\f, and lay out the post with line breaks only"
    ]


def _source_exists(source: str, moments: int) -> bool:
    kind, _, ref = source.partition(":")
    if kind == "key_moment":
        return ref.isdecimal() and 1 <= int(ref) <= moments
    return kind == "catalog" and ref in CATALOG_FIELDS


def _quote_problems(text: str, title: str, claims: list[Claim], key_moments: list[KeyMoment]) -> list[str]:
    cited = {c.source for c in claims}
    problems = []
    for groups in QUOTED.findall(text):
        span = next(g for g in groups if g).strip(OUTER_PUNCTUATION)
        if not span or span == title:
            continue
        word_for_word = whole_words(span)
        sources = [
            f"key_moment:{i}"
            for i, m in enumerate(key_moments, 1)
            if m.quote and word_for_word.search(nfc(m.quote.text))
        ]
        if not sources:
            problems.append(
                f"quote {span!r} is not a Key Moment's line word for word; quote it exactly, or don't quote"
            )
        elif not cited.intersection(sources):
            problems.append(f"quote {span!r} is from {' or '.join(sources)}; cite it in the claims")
    return problems


def _vietnamese(text: str) -> bool:
    decomposed = unicodedata.normalize("NFD", text)
    return "đ" in text.casefold() or any(ch in VIETNAMESE_MARKS for ch in decomposed)

"""The brand rules code can check, from prompts/brand.md: no shouting, few emojis, a call to watch.

The rest of brand.md (clickbait, tone, invented details) needs judgment and is the
Checker's. These run inside tracing, so the Copywriter fixes them before the
Checker ever sees the post. The Checker is told code has checked them, so they
must be right: an emoji is counted as a person sees it, and the call to watch must
be the Copywriter's own words at the end of the post, not a title or a quote.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from trailer_to_campaign.agents.post_text import own_words

MAX_EMOJIS = 2
SHOUT_MIN_LETTERS = 4  # TV, USA, OK are not shouting
CALL_TO_WATCH = {
    "en": re.compile(r"\b(stream|streaming|watch)\b", re.IGNORECASE),
    "vi": re.compile(r"\bxem\b", re.IGNORECASE),
}

WORD = re.compile(r"[^\W\d_]+")
SENTENCE_END = re.compile(r"[.!?\n]+")

# One emoji as a person sees it: a flag (two regional letters), or a pictograph with an
# optional skin tone and presentation selector, joined to others by zero-width joiners.
_PICTOGRAPH = "[⌀-⏿☀-➿⬀-⯿\U0001f000-\U0001f1e5\U0001f200-\U0001f3fa\U0001f400-\U0001faff]"
_ONE = f"{_PICTOGRAPH}[\U0001f3fb-\U0001f3ff]?️?"
EMOJI = re.compile(f"[\U0001f1e6-\U0001f1ff]{{2}}|{_ONE}(?:‍{_ONE})*")


def brand_problems(text: str, language: str, title: str, allowed_caps: Iterable[str] = ()) -> list[str]:
    """allowed_caps: words that may be in capitals because the catalog writes them so (IMAX, NASA)."""
    problems = []
    mine = own_words(text, title)  # a quoted line or the title keeps its capitals
    allowed = set(allowed_caps)
    shouted = [w for w in WORD.findall(mine) if len(w) >= SHOUT_MIN_LETTERS and w.isupper() and w not in allowed]
    if shouted:
        problems.append(f"no ALL CAPS: {', '.join(dict.fromkeys(shouted))}")
    emojis = len(EMOJI.findall(text))
    if emojis > MAX_EMOJIS:
        problems.append(f"at most {MAX_EMOJIS} emojis; this post has {emojis} emojis")
    call = CALL_TO_WATCH.get(language)
    sentences = [s for s in SENTENCE_END.split(mine) if WORD.search(s)]
    if call and not (sentences and call.search(sentences[-1])):
        problems.append('end with a call to watch the Film on our platform ("Stream it now", "Xem ngay")')
    return problems

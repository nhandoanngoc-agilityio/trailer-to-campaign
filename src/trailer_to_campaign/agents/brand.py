"""The brand rules code can check, from prompts/brand.md: no shouting, few emojis, a call to watch.

The rest of brand.md (clickbait, tone, invented details) needs judgment and is the
Checker's. These run inside trace(), so the Copywriter fixes them before the
Checker ever sees the post.
"""

from __future__ import annotations

import re

MAX_EMOJIS = 2
SHOUT_MIN_LETTERS = 4  # TV, USA, OK are not shouting
CALL_TO_WATCH = {"en": re.compile(r"\b(stream|watch)", re.IGNORECASE), "vi": re.compile(r"\bxem\b", re.IGNORECASE)}

WORD = re.compile(r"[^\W\d_]+")
EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿]")


def brand_problems(text: str, language: str, title: str) -> list[str]:
    problems = []
    title_words = set(WORD.findall(title))
    shouted = [w for w in WORD.findall(text) if len(w) >= SHOUT_MIN_LETTERS and w.isupper() and w not in title_words]
    if shouted:
        problems.append(f"no ALL CAPS: {', '.join(dict.fromkeys(shouted))}")
    emojis = len(EMOJI.findall(text))
    if emojis > MAX_EMOJIS:
        problems.append(f"at most {MAX_EMOJIS} emojis; this post has {emojis} emojis")
    call = CALL_TO_WATCH.get(language)
    if call and not call.search(text):
        problems.append('end with a call to watch the Film on our platform ("Stream it now", "Xem ngay")')
    return problems

"""How the checks read a post's text, in one place so they always agree.

nfc: the same letters can be one code point or two (Vietnamese especially); every
check compares text in NFC. own_words: what the Copywriter wrote itself, without
the title, quoted lines, glosses and hashtags it was given or required to use.
"""

from __future__ import annotations

import re
import unicodedata

# Quoted lines in any quotation marks a post may use. A closing ’ must end a word,
# so an apostrophe inside ‘I’m going in’ doesn't close the quote.
QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”|«([^»]+)»|‘(.+?)’(?!\w)|„([^“]+)“|「([^」]+)」')
GLOSS = re.compile(r"\(\s*tạm dịch:[^)]*\)", re.IGNORECASE)
HASHTAG = re.compile(r"#\w+")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def whole_words(text: str) -> re.Pattern[str]:
    """A pattern matching text only as whole words, not inside a longer word."""
    return re.compile(rf"(?<!\w){re.escape(text)}(?!\w)")


def own_words(text: str, title: str) -> str:
    """The post without its title, quotes, glosses and hashtags: the words the Copywriter wrote itself."""
    for pattern in (whole_words(title), QUOTED, GLOSS, HASHTAG):
        text = pattern.sub(" ", text)
    return text

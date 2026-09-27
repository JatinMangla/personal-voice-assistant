"""Spot "remember that ..." / "forget that ..." commands (English, Hindi, Hinglish)."""
import re

from config import ASSISTANT_NAME

_LEADING = re.compile(rf"^\s*(?:(?:hey|ok|okay|hi)\s+)?(?:{ASSISTANT_NAME}\s*)?[,!.:]?\s*", re.I)
_TRAILING = re.compile(r"[\s।.!?,]+$")

_HI_REMEMBER = r"(?:याद\s+रखना|याद\s+रखो|याद\s+रखिए|yaad\s+rakhna|yaad\s+rakho|yad\s+rakhna)"
_HI_FORGET = r"(?:भूल\s+जाओ|भूल\s+जाना|भूल\s+जाइए|bhool\s+jao|bhul\s+jao|bhool\s+jana)"

_PATTERNS = [
    ("remember", re.compile(
        r"^(?:please\s+)?remember(?!\s+(?:when|what|who|where|how|why|if|me)\b)(?:\s+that)?\s+(.+)$",
        re.I)),
    ("remember", re.compile(rf"^{_HI_REMEMBER}\s*(?:कि|ki)?\s+(.+)$", re.I)),
    ("remember", re.compile(rf"^(.+?)\s*,?\s*{_HI_REMEMBER}$", re.I)),
    ("forget", re.compile(r"^(?:please\s+)?forget(?:\s+that|\s+about)?\s+(.+)$", re.I)),
    ("forget", re.compile(rf"^{_HI_FORGET}\s*(?:कि|ki)?\s+(.+)$", re.I)),
    ("forget", re.compile(rf"^(.+?)\s*,?\s*{_HI_FORGET}$", re.I)),
]


def parse_command(text: str) -> tuple[str, str] | None:
    """Return ("remember" | "forget", content) or None for normal chat."""
    cleaned = _TRAILING.sub("", _LEADING.sub("", text.strip()))
    for action, pattern in _PATTERNS:
        match = pattern.match(cleaned)
        if match:
            content = _TRAILING.sub("", match.group(1).strip())
            if content:
                return action, content
    return None

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


# --- yes / no, for actions that wait for the user's spoken confirmation (tools.py) -------
_YES = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "do it", "go ahead", "please do",
    "yes please", "yes do it", "yes go ahead", "confirm", "confirmed", "of course",
    "haan", "haa", "han", "ha", "haan ji", "han ji", "ji haan", "ji han", "haan kar do",
    "kar do", "kardo", "karo", "theek hai", "thik hai", "bilkul", "zaroor",
    "हाँ", "हां", "हाँ जी", "हां जी", "जी हाँ", "जी हां", "कर दो", "करो", "ठीक है", "बिल्कुल",
    "ज़रूर", "जरूर", "हाँ कर दो", "हां कर दो",
}
_NO = {
    "no", "nope", "nah", "don't", "do not", "cancel", "stop", "no thanks", "not now",
    "never mind", "nevermind", "leave it", "no don't",
    "nahi", "nahin", "na", "mat karo", "rehne do", "rahne do", "nahi karo", "cancel karo",
    "नहीं", "ना", "मत करो", "रहने दो", "नहीं करो", "कैंसल करो",
}


def parse_confirmation(text: str) -> str | None:
    """"yes", "no", or None when the words are anything else (then the waiting action is
    dropped). Only a short, plain answer counts: "yes, but first tell me..." is not a yes."""
    cleaned = _TRAILING.sub("", _LEADING.sub("", text.strip())).lower()
    cleaned = " ".join(re.sub(r"[,.!?।]", " ", cleaned).split())
    for prefix in ("yes ", "haan ", "हाँ ", "हां ", "ok ", "okay "):
        if cleaned.startswith(prefix) and cleaned[len(prefix):] in _YES:
            return "yes"  # "yes do it", "haan kar do"
    if cleaned in _YES:
        return "yes"
    if cleaned in _NO:
        return "no"
    return None

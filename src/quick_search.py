"""Songs, videos and prices are searched BEFORE the model answers, by fixed rules.

Why: the model decides by itself whether to call find_video / find_prices, and sometimes it
doesn't: it then says "I'm playing it" with nothing on the screen (seen 2026-10-01 with
"play an Arijit song"). For these requests the search now always runs first, the cards are
shown at once, and the model answers from the results (it can still search more).

detect() understands English, Hindi and Hinglish, e.g.:
  "play Arijit Singh's latest song", "Kesariya bajao", "यूट्यूब पर भजन चलाओ",
  "suggest some romantic songs", "price of boAt Airdopes 141", "iPhone 16 kitne ka hai".
Nothing is sent anywhere by this module; it only reads the words.
"""
import re
from dataclasses import dataclass

from config import ASSISTANT_NAME


def _words(*words: str) -> str:
    """Whole words only, for Devanagari too (regex word boundaries break on Hindi vowel signs)."""
    return r"(?<!\S)(?:" + "|".join(words) + r")(?!\S)"


@dataclass
class Search:
    tool: str          # "find_video" or "find_prices"
    arguments: dict
    play: bool = False  # the user asked to PLAY it: the app opens the first video by itself


_LEADING = re.compile(
    rf"^\s*(?:(?:hey|ok|okay|hi|hello)\s+)?(?:{ASSISTANT_NAME}\s*)?[,!.:]?\s*", re.I)
_PUNCT = re.compile(r"[?!.,।\"“”]+")

# "play" as a verb (not "play a game", "play store", "screenplay")
_PLAY_EN = r"\b(?:play|put on|start playing)\b(?!\s+(?:a\s+)?(?:game|games|with me|chess|quiz|store)\b)"
_PLAY_HI = (r"\b(?:bajao|baja\s*do|bajaiye|bajana|chalao|chala\s*do|chalaiye|sunao|suna\s*do|"
            r"sunaiye|lagao|laga\s*do|dikhao|dikha\s*do)\b|बजाओ|बजा\s*दो|बजाइए|बजाना|चलाओ|चला\s*दो|"
            r"चलाइए|सुनाओ|सुना\s*दो|सुनाइए|लगाओ|लगा\s*दो|दिखाओ|दिखा\s*दो")
_MEDIA = (r"\b(?:songs?|music|video|videos|youtube|trailer|playlist|bhajans?|gaana|gaane|gana|"
          r"gane|geet|qawwali|ghazal)\b|गाना|गाने|गीत|भजन|वीडियो|यूट्यूब|संगीत|ग़ज़ल|कव्वाली")
_SUGGEST = (r"\b(?:suggest|recommend|recommendations?|some|best|top|good|latest|new|newest|"
            r"popular|trending|link|links)\b|सुझाओ|अच्छे|नए|नया|लेटेस्ट")
_LATEST = r"\b(?:latest|new|newest|recent|naya|naye|nayi)\b|नया|नए|नई|लेटेस्ट"

_PRICE = (r"\b(?:price|prices|pricing|cost of|costs?|cheapest|best deal|kitne\s+ka|kitna\s+ka|"
          r"kitne\s+ki|kitne\s+mein|daam|dam|keemat|kimat|rate of)\b|कीमत|क़ीमत|दाम|कितने\s+का|"
          r"कितने\s+की|कितने\s+में|सबसे\s+सस्ता|सस्ता")
# Questions about Jarvis's own limits or the user's bills are not shopping.
_NOT_SHOPPING = (r"\b(?:bill|bills|paid|pay|gpu|limit|limits|ngrok|electricity|rent|emi|"
                 r"subscription)\b|बिल|भुगतान")

# "Suggest a good laptop", "recommend restaurants in Noida", "कोई अच्छी किताब सुझाओ":
# searched first too, so the answer comes with link cards.
_ADVICE = (r"\b(?:suggest|suggestions?|recommend|recommendations?|sujhao|sujhav|batao koi)\b|"
           r"सुझाओ|सुझाव|सुझाइए")
_FILLER_ADVICE = (
    r"\b(?:please|pls|can you|could you|would you|will you|me|mujhe|some|a few|few|any|koi|"
    r"kuch|suggest|suggestions?|recommend|recommendations?|sujhao|sujhav|batao|for me)\b",
    _words("मुझे", "कुछ", "कोई", "सुझाओ", "सुझाव", "सुझाइए", "दो", "दीजिए"),
)

_FILLER_VIDEO = (
    _PLAY_EN, _PLAY_HI,
    r"\b(?:please|pls|plz|can you|could you|would you|will you|for me|me|mujhe|mere liye|zara|"
    r"jara|kar\s*do|karo|do|now|abhi|some|suggest|recommend|a few|few)\b",
    r"\b(?:on|in|from|pe|par|par se)\s+(?:youtube|you tube)\b|\byoutube\s+(?:pe|par|on)\b",
    r"यूट्यूब\s+(?:पर|पे)", _words("मुझे", "ज़रा", "जरा", r"कर\s+दो", "प्लीज़", "सुझाओ", "कुछ"),
    r"\b(?:the\s+)?(?:youtube\s+)?(?:video|link)\s+(?:of|for)\b",
)
_FILLER_PRICE = (
    r"\b(?:what(?:'s| is| are)?|tell me|find|check|search|show|me|the|a|an|please|pls|"
    r"current|today'?s?|latest|online|in india|india|buy|to buy|where|can i|how much|does|do|"
    r"is|are|it|of|for|cost|costs|price|prices|pricing|cheapest|best deal|rate|kitne|kitna|"
    r"ka|ki|ke|hai|mein|milega|milegi|batao|bataiye|bata do|daam|dam|keemat|kimat|kya|sabse|"
    r"sasta|saste|abhi)\b",
    _words("कीमत", "क़ीमत", "दाम", "कितने", "कितना", "का", "की", "के", "है", "में", "मिलेगा",
           "बताओ", "बताइए", "क्या", "सबसे", "सस्ता", "सस्ते", "अभी"),
)


def _has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.I) is not None


def _strip(text: str, patterns: tuple[str, ...]) -> str:
    for p in patterns:
        text = re.sub(p, " ", text, flags=re.I)
    return " ".join(text.split()).strip(" -:")


def detect(text: str) -> Search | None:
    """A song/video or price request, with the search words; None for anything else."""
    cleaned = _PUNCT.sub(" ", _LEADING.sub("", text.strip()))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    play = _has(_PLAY_EN, cleaned) or _has(_PLAY_HI, cleaned)
    media = _has(_MEDIA, cleaned)
    youtube = _has(r"\byoutube\b|यूट्यूब", cleaned)
    if play or youtube or (media and _has(_SUGGEST, cleaned)):
        query = _strip(cleaned, _FILLER_VIDEO)
        if len(query) >= 2:
            return Search("find_video", {"query": query[:200], "latest": _has(_LATEST, cleaned)},
                          play=play and not _has(r"\b(?:suggest|recommend)\b|सुझाओ", cleaned))
    if _has(_PRICE, cleaned) and not _has(_NOT_SHOPPING, cleaned):
        product = _strip(cleaned, _FILLER_PRICE)
        if len(product) >= 2:
            return Search("find_prices", {"product": product[:150]})
    if _has(_ADVICE, cleaned) and not _has(_NOT_SHOPPING, cleaned):
        query = _strip(cleaned, _FILLER_ADVICE)
        if len(query) >= 3:
            return Search("web_search", {"query": query[:200]})
    return None

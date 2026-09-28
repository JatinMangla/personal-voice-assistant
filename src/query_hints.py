"""Cheap rules that read a question before the memory search (no LLM call, < 1 ms).

- date range: "today", "yesterday", "last week", "in March", "3 days ago", "पिछले हफ्ते",
  "pichhle mahine" ... -> only conversations from that time are searched;
- categories: "my decisions", "health", "goals" ... -> those facts are shown first;
- recall: is the user asking about the past ("what did I say", "maine kya bataya")?
  Then a date alone is enough to fetch that day's conversations;
- keywords: the distinctive words for the keyword (FTS5) search.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# Words: Latin letters/digits plus the whole Devanagari block (its vowel signs are not
# "letters" to Python, and splitting on them would break Hindi words apart).
_WORD = re.compile(r"[0-9A-Za-zÀ-ɏऀ-ॿ]+")

STOPWORDS = set("""
a an the and or but if of to in on at by for from with about as into over after before
is am are was were be been being do does did done have has had having will would shall
should can could may might must i me my mine myself we us our ours you your yours he him
his she her hers it its they them their theirs this that these those there here what which
who whom whose when where why how all any some no not only just also very so than too
please tell know say said ask asked let lets get got give make go going want need like
jarvis hey ok okay hi hello yes yeah thanks thank one two about again much many more most
today yesterday tomorrow week weeks month months year years day days ago last past this
next earlier recently time remember remind talk talked told discuss discussed
hai hain ho tha thi the ka ki ke ko se me mein main mera meri mere mujhe hum hamara humne
maine kya kaun kab kaise kyun kahan aur bhi to toh ye yeh wo woh aap tum tumne kuch ek
batao bataiye bataya bolo kaha tha baat ki kal aaj parso pichhle pichle pichhla hafte
hafta mahine mahina saal din pehle
है हैं हो था थी थे का की के को से में मैं मेरा मेरी मेरे मुझे मुझको हम हमारा हमने मैंने
क्या कौन कब कैसे क्यों कहाँ कहां और भी तो ये यह वो वह आप तुम तुमने कुछ एक बताओ बताइए
बताया बोलो कहा बात कल आज परसों पिछले पिछला इस हफ्ते हफ़्ते महीने साल दिन पहले याद
""".split())

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12,
    "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6,
    "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "अक्तूबर": 10,
    "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
            "saturday": 5, "sunday": 6, "सोमवार": 0, "मंगलवार": 1, "बुधवार": 2,
            "गुरुवार": 3, "शुक्रवार": 4, "शनिवार": 5, "रविवार": 6}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5, "एक": 1, "दो": 2, "तीन": 3,
           "चार": 4, "पांच": 5, "पाँच": 5}

CATEGORY_WORDS = {
    "decision": r"decision|decided|decide|faisla|faisle|फ़ैसल|फैसल|निर्णय",
    "health": r"health|allerg|medicine|medication|tablet|doctor|disease|illness|condition"
              r"|सेहत|स्वास्थ्य|दवा|दवाई|एलर्जी|बीमारी|bimari|dawai|sehat",
    "diet": r"diet|vegetarian|vegan|eat\b|food|khana|शाकाहारी|परहेज|खाना",
    "goal": r"goal|plan\b|plans|target|aim\b|लक्ष्य|इरादा|yojana|योजना",
    "work": r"\bwork|job|office|career|project|kaam|naukri|काम|नौकरी|दफ्तर|ऑफिस",
    "person": r"family|wife|husband|mother|father|\bmom\b|\bdad\b|\bson\b|daughter|brother"
              r"|sister|friend|parivar|परिवार|पत्नी|पति|माँ|मां|पापा|भाई|बहन|दोस्त",
    "routine": r"habit|routine|aadat|आदत|दिनचर्या",
    "preference": r"prefer|favourite|favorite|pasand|पसंद",
    "event": r"event|happened|trip|bought|ghatna|घटना",
    "place": r"\bplace|city|home town|hometown|shahar|शहर",
    "pattern": r"focus|pattern|usually|often",
}
_CATEGORY_RES = {cat: re.compile(rx, re.IGNORECASE) for cat, rx in CATEGORY_WORDS.items()}

_RECALL = re.compile(
    r"\b(did i|did we|have i|had i|i told|i said|i asked|i mentioned|we talked|we discussed"
    r"|we spoke|talked about|did you|you said|you told|remember when|what did|when did"
    r"|last time|earlier|previously|our conversation|we chat)"
    r"|\b(maine|mainey|humne|hamne|baat ki|baat hui|bataya|kaha tha|pucha|poocha|yaad hai)\b"
    r"|मैंने|हमने|बताया|बात की|बात हुई|कहा था|पूछा|याद है",
    re.IGNORECASE)


@dataclass
class Hints:
    date_range: tuple[date, date] | None = None   # [start, end): end is exclusive
    categories: set[str] = field(default_factory=set)
    recall: bool = False
    terms: list[str] = field(default_factory=list)  # FTS5 terms, prefix terms end in "*"


def _month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start, end


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def date_range(text: str, today: date, recall: bool) -> tuple[date, date] | None:
    """The time the question is about, or None. Checked most specific first."""
    t = text.lower()
    one = lambda d: (d, d + timedelta(days=1))  # noqa: E731

    if re.search(r"day before yesterday|parso|परसों", t):
        return one(today - timedelta(days=2))
    m = re.search(r"(\d+|[a-z]+|[ऀ-ॿ]+)\s+(days?|din|दिन)\s+(ago|pehle|pahle|पहले)", t)
    if m and (n := int(m.group(1)) if m.group(1).isdigit() else NUMBERS.get(m.group(1))):
        return one(today - timedelta(days=n))
    m = re.search(r"(?:last|past|pichhle|pichle|पिछले)\s+(\d+|[a-z]+|[ऀ-ॿ]+)\s+(days|din|दिन)", t)
    if m and (n := int(m.group(1)) if m.group(1).isdigit() else NUMBERS.get(m.group(1))):
        return today - timedelta(days=n), today + timedelta(days=1)
    if re.search(r"\byesterday\b", t) or (recall and re.search(r"\bkal\b|कल", t)):
        return one(today - timedelta(days=1))
    if re.search(r"\btoday\b|\baaj\b|आज", t):
        return one(today)
    if re.search(r"last week|previous week|pichhle hafte|pichle hafte|पिछले ह(फ्|फ़्)ते", t):
        start = _week_start(today) - timedelta(days=7)
        return start, start + timedelta(days=7)
    if re.search(r"this week|is hafte|इस ह(फ्|फ़्)ते", t):
        return _week_start(today), today + timedelta(days=1)
    if re.search(r"last month|previous month|pichhle mahine|pichle mahine|पिछले महीने", t):
        prev = today.replace(day=1) - timedelta(days=1)
        return _month_range(prev.year, prev.month)
    if re.search(r"this month|is mahine|इस महीने", t):
        return today.replace(day=1), today + timedelta(days=1)
    if re.search(r"last year|pichhle saal|pichle saal|पिछले साल", t):
        return date(today.year - 1, 1, 1), date(today.year, 1, 1)
    if re.search(r"this year|is saal|इस साल", t):
        return date(today.year, 1, 1), today + timedelta(days=1)
    for word in _WORD.findall(t):
        if word in MONTHS and (word not in ("may", "mar", "dec", "jun", "jul") or recall
                               or re.search(rf"\b(in|during|of|since)\s+{word}\b", t)):
            month = MONTHS[word]
            year_m = re.search(rf"{word}\s+((?:19|20)\d\d)\b", t)
            year = int(year_m.group(1)) if year_m else (
                today.year if month <= today.month else today.year - 1)
            return _month_range(year, month)
    for word, weekday in WEEKDAYS.items():
        if re.search(rf"(?:\b|^){word}", t):
            back = (today.weekday() - weekday) % 7 or (7 if re.search(r"last|pichhle|पिछले", t) else 0)
            return one(today - timedelta(days=back))
    return None


def keyword_terms(text: str) -> list[str]:
    """Distinctive words as FTS5 terms. Long Latin words become prefixes, so
    "allergic" also finds "allergy" and "peanuts" finds "peanut"."""
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word in STOPWORDS or word in MONTHS or word in WEEKDAYS:
            continue
        if word.isascii() and word.isalpha():
            if len(word) < 3:
                continue
            term = f"{word[:max(5, len(word) - 2)]}*" if len(word) >= 6 else word
        else:
            term = word
        if term not in terms:
            terms.append(term)
    return terms[:12]


def parse(text: str, now: datetime) -> Hints:
    recall = bool(_RECALL.search(text))
    return Hints(
        date_range=date_range(text, now.date(), recall),
        categories={cat for cat, rx in _CATEGORY_RES.items() if rx.search(text)},
        recall=recall,
        terms=keyword_terms(text),
    )

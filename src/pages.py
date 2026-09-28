"""Read public web pages safely: link previews (thumbnail, title, site) and prices.

Used for the WhatsApp-style link cards and the price search. Safety:
- only public http(s) addresses (never private, local or cloud-internal ones, also
  not through redirects), so search results can't reach inside the Kaggle machine;
- at most MAX_BYTES read per page, short timeouts, text only;
- page content is data: it is never followed as instructions.
"""
import ipaddress
import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

USER_AGENT = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Mobile Safari/537.36")
MAX_BYTES = 400_000
_PRICE_TEXT = re.compile(r"(₹|Rs\.?|INR)\s?([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_YOUTUBE_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([\w-]{11})")


def youtube_id(url: str) -> str | None:
    if "youtube.com" in url or "youtu.be" in url:
        match = _YOUTUBE_ID.search(url)
        return match.group(1) if match else None
    return None


def is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def fetch_html(url: str, timeout: float = 4) -> str | None:
    """The start of a public HTML page, or None."""
    for _ in range(4):  # follow redirects ourselves, checking every hop
        if not is_public_url(url):
            return None
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT,
                                              "Accept-Language": "en-IN,en;q=0.9"},
                                timeout=(3, timeout), stream=True, allow_redirects=False)
        except requests.RequestException:
            return None
        with resp:
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                return None
            data = b""
            for chunk in resp.iter_content(32_768):
                data += chunk
                if len(data) >= MAX_BYTES:
                    break
            return data.decode(resp.encoding or "utf-8", errors="ignore")
    return None


class _MetaParser(HTMLParser):
    """Collects <meta> tags, <title> and JSON-LD blocks."""

    def __init__(self):
        super().__init__()
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.title = ""
        self._in_jsonld = self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self.jsonld.append("")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_jsonld = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld[-1] += data
        elif self._in_title and len(self.title) < 200:
            self.title += data


def _offers(node):
    """Yield (price, currency) from JSON-LD product data."""
    if isinstance(node, list):
        for item in node:
            yield from _offers(item)
    elif isinstance(node, dict):
        offers = node.get("offers")
        if offers is not None:
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    price = offer.get("price") or offer.get("lowPrice")
                    if price is not None:
                        yield price, offer.get("priceCurrency")
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _offers(value)


def _to_number(value) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
        return number if number > 0 else None
    except ValueError:
        return None


def price_from_text(text: str) -> float | None:
    """₹12,999 / Rs. 12,999 / INR 12999 in plain text."""
    match = _PRICE_TEXT.search(text or "")
    return _to_number(match.group(2)) if match else None


def format_price(amount: float, currency: str | None = "INR") -> str:
    if (currency or "INR").upper() in ("INR", "₹", "RS"):
        return f"₹{amount:,.0f}"
    return f"{currency} {amount:,.2f}"


def preview(url: str) -> dict:
    """{image, title, description, site, price, currency} from a page (missing keys = unknown)."""
    html = fetch_html(url)
    if not html:
        return {}
    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    m = parser.meta
    out = {
        "title": (m.get("og:title") or m.get("twitter:title") or parser.title).strip()[:120],
        "description": (m.get("og:description") or m.get("description") or "").strip()[:160],
        "site": (m.get("og:site_name") or "").strip()[:40],
    }
    image = m.get("og:image") or m.get("og:image:secure_url") or m.get("twitter:image")
    if image:
        image = urljoin(url, image)
        if image.startswith("https://"):  # http images are blocked on https pages
            out["image"] = image[:500]
    amount = _to_number(m.get("product:price:amount") or m.get("og:price:amount")
                        or m.get("price"))
    currency = m.get("product:price:currency") or m.get("og:price:currency") or m.get("pricecurrency")
    if amount is None:
        for block in parser.jsonld:
            try:
                for value, cur in _offers(json.loads(block)):
                    amount, currency = _to_number(value), cur
                    if amount:
                        break
            except ValueError:
                continue
            if amount:
                break
    if amount:
        out["price"], out["currency"] = amount, currency or "INR"
    return {k: v for k, v in out.items() if v}


def previews(urls: list[str]) -> list[dict]:
    """preview() for several pages at once (in parallel)."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=min(5, len(urls))) as pool:
        return list(pool.map(lambda u: _safe_preview(u), urls))


def _safe_preview(url: str) -> dict:
    try:
        return preview(url)
    except Exception:
        return {}

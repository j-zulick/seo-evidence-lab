"""Deterministic UTF-8 extraction and feed records."""
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..contracts import canonical_bytes
from .http import RetrievalError

ATOM = "{http://www.w3.org/2005/Atom}"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = None
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if not self.hidden and tag in {"script", "style", "template", "head"}:
            self.hidden = tag

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def timestamp(value):
    if not value:
        return None
    try:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def feed_records(text, url):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise RetrievalError("unsafe_xml")
    root = ET.fromstring(text)
    if root.tag == ATOM + "feed":
        entries = root.findall(ATOM + "entry")
        prefix = ATOM
    elif root.tag == "rss" and root.find("channel") is not None:
        entries = root.findall("./channel/item")
        prefix = ""
    else:
        raise RetrievalError("unsupported_feed")

    documents = []
    for entry in entries:
        def value(name):
            node = entry.find(prefix + name)
            return "".join(node.itertext()).strip() if node is not None else ""

        link = value("link")
        if prefix:
            link = next((node.get("href", "") for node in entry.findall(ATOM + "link")
                         if node.get("rel", "alternate") == "alternate"), "")
        document_url = None
        if link and not any(ord(c) < 33 or c == "\\" for c in link):
            candidate = urljoin(url, link)
            parts = urlsplit(candidate)
            if parts.scheme in {"https", "http"} and parts.hostname and not parts.username and not parts.password:
                document_url = candidate
        identifier = value("id" if prefix else "guid") or document_url
        title = value("title")
        summary = value("summary") or value("content") if prefix else value("description")
        if not (identifier or title or summary):
            raise RetrievalError("empty_feed_item")
        published = value("published" if prefix else "pubDate")
        updated = value("updated") if prefix else ""
        documents.append(dict(id=identifier, url=document_url, title=title, summary=summary,
                              published_at=timestamp(published), updated_at=timestamp(updated),
                              published_raw=published or None, updated_raw=updated or None))
    return documents


def extract(method, body, url):
    if not body:
        raise RetrievalError("missing_body")
    try:
        text = body.decode("utf-8-sig")
        if method == "feed":
            records = feed_records(text, url)
            return canonical_bytes(records), records
        if method == "json":
            text = canonical_bytes(json.loads(text)).decode("utf-8")
        elif method == "html":
            parser = VisibleText()
            parser.feed(text)
            parser.close()
            text = " ".join(" ".join(parser.chunks).split())
        else:
            text = text.strip()
        if not text:
            raise RetrievalError("missing_text")
        return text.encode("utf-8"), [dict(published_at=None, updated_at=None)]
    except (UnicodeError, ET.ParseError, ValueError, RecursionError):
        raise RetrievalError("parse_error") from None

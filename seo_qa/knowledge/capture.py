"""Explicit, one-shot ingestion of approved knowledge sources."""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from math import isfinite
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

from .contracts import Source, SourceSnapshot
from .ingestion.extract import extract, timestamp
from .ingestion.http import Response, RetrievalError, approved_host, check_body, https_get
from .ingestion.store import SnapshotStore

_REDIRECTS = {301, 302, 303, 307, 308}
_RETRY = {"timeout", "transport_error", "http_429", "http_500", "http_502",
          "http_503", "http_504"}
_SAFE_HEADERS = {"cache-control", "content-encoding", "content-length", "content-range",
                 "content-type", "date", "etag", "last-modified", "location", "retry-after",
                 "transfer-encoding", "vary"}


@dataclass(frozen=True)
class ApprovedSource:
    source: Source
    method: Literal["feed", "html", "json", "text"]
    redirect_hosts: tuple[str, ...] = ()
    enabled: bool = True
    min_interval: float = 1.0


class ByteTransport(Protocol):
    def get(self, url: str, headers: dict[str, str], deadline: float, max_bytes: int) -> Response:
        """Return transfer-decoded body bytes using a monotonic deadline."""
        ...


class Fixtures:
    """Scripted responses, indexed by exact URL. Exhaustion fails closed."""
    def __init__(self, responses):
        self._responses = {url: iter(items) for url, items in responses.items()}
        self._lock = threading.Lock()
        self.requests = []

    def get(self, url, headers, deadline, max_bytes):
        with self._lock:
            self.requests.append((url, dict(headers)))
            reply = next(self._responses.get(url, iter(())), None)
        if reply is None:
            raise RetrievalError("fixture_missing")
        if isinstance(reply, Exception):
            raise reply
        return reply


class Ingestor:
    def __init__(self, database: Path, sources, *, fixtures: ByteTransport | None = None,
                 live: bool = False, workers: int = 4, timeout: float = 10,
                 retries: int = 2, max_bytes: int = 2_000_000, observed_clock=None):
        if live == (fixtures is not None):
            raise ValueError("Select exactly one of fixture mode or live HTTPS")
        if (not 1 <= workers <= 16 or not 0 < timeout <= 60 or
                not 0 <= retries <= 4 or not 1 <= max_bytes <= 16_000_000):
            raise ValueError("Limits outside supported bounds")
        self._sources = {}
        for policy in sources:
            source = Source.model_validate(policy.source)
            if source.id in self._sources or policy.method not in {"feed", "html", "json", "text"}:
                raise ValueError("Duplicate source or unsupported retrieval method")
            if not isfinite(policy.min_interval) or policy.min_interval < 0:
                raise ValueError("Invalid rate limit")
            if live and policy.min_interval < 0.1:
                raise ValueError("Live sources require at least 100 ms between requests")
            self._sources[source.id] = policy
        self.store = SnapshotStore(database)
        self._clock = observed_clock or (lambda: datetime.now(timezone.utc))
        self._mode = "https" if live else "fixture"
        self._get = https_get if live else fixtures.get
        self._workers = workers
        self._slots = threading.BoundedSemaphore(workers)
        self._timeout = timeout
        self._retries = retries
        self._max_bytes = max_bytes

    def run(self, source_ids):
        """Yield results in input order with at most one batch queued."""
        ids = iter(source_ids)
        with ThreadPoolExecutor(max_workers=self._workers) as executor:
            while batch := list(islice(ids, self._workers)):
                yield from executor.map(self.fetch, batch)

    def fetch(self, source_id):
        """Persist one observation, including failures, and return its diagnostics."""
        policy = self._sources[source_id]
        with self._slots:
            return self._fetch(policy)

    def capture(self, source_id):
        """Return the frozen snapshot, raw bytes, text bytes and observation receipt."""
        receipt = self.fetch(source_id)
        if receipt["snapshot_id"]:
            return (*self.store.read(receipt["snapshot_id"]), receipt)
        return SourceSnapshot.model_validate(receipt["snapshot"]), None, None, receipt

    def health(self, source_id):
        policy = self._sources[source_id]
        return self.store.health(source_id, policy.method, self._mode)

    def _fetch(self, policy):
        source = policy.source
        previous = self.store.previous(source.id, policy.method, self._mode)
        attempts = []
        reason = "source_disabled"
        capture = None
        rejected_body = None
        reuse = None
        if policy.enabled and source.status != "retracted":
            for retry in range(self._retries + 1):
                try:
                    result, final_url, sent = self._request(policy, previous, attempts)
                    if result.status == 304:
                        if not sent:
                            raise RetrievalError("unexpected_304")
                        returned = result.header("etag")
                        expected = sent.get("If-None-Match")
                        if returned and expected and returned.removeprefix("W/") != expected.removeprefix("W/"):
                            raise RetrievalError("validator_mismatch")
                        reuse = previous
                        reason = "not_modified"
                    elif result.status == 200:
                        length = check_body(result, self._max_bytes)
                        if len(result.body) > self._max_bytes:
                            raise RetrievalError("body_too_large")
                        if length is not None and len(result.body) != length:
                            raise RetrievalError("truncated_body")
                        rejected_body = result.body
                        text, documents = extract(policy.method, result.body, final_url)
                        rejected_body = None
                        capture = (result.body, text, documents)
                        reason = "captured"
                    else:
                        raise RetrievalError(f"http_{result.status}")
                    break
                except RetrievalError as exc:
                    reason = str(exc)
                    if reason not in _RETRY or retry == self._retries:
                        break
                    # Retry-After also persists across separate runs.
                    time.sleep(min(2 ** retry * 0.25, self._timeout))
        observed = self._clock()
        if observed.utcoffset() is None:
            raise ValueError("Observation time must include a timezone")
        observed = observed.astimezone(timezone.utc)
        return self.store.save(source.id, policy.method, self._mode, observed, reason,
                               attempts, capture, reuse, rejected_body)

    @staticmethod
    def _valid_validator(field, value):
        if field == "etag":
            return re.fullmatch(r'(?:W/)?"[!#-~]*"', value) is not None
        return (len(value) <= 40 and all(32 <= ord(char) < 127 for char in value)
                and timestamp(value) is not None)

    def _request(self, policy, previous, attempts):
        url = policy.source.canonical_url
        hosts = {urlsplit(url).hostname, *policy.redirect_hosts}
        deadline = time.monotonic() + self._timeout
        for hop in range(4):
            headers = {"Accept-Encoding": "identity", "User-Agent": "SEO-QA-K02/1"}
            sent = {}
            if previous and url == previous["url"]:
                for field, header in (("etag", "If-None-Match"),
                                      ("last-modified", "If-Modified-Since")):
                    value = previous["validators"].get(field)
                    if value and self._valid_validator(field, value):
                        sent[header] = value
                headers.update(sent)
            response = None
            error = None
            started = datetime.now(timezone.utc).isoformat()
            try:
                approved_host(url, hosts)
                self.store.take_slot(policy.source.id, self._mode, policy.min_interval, deadline)
                response = self._get(url, headers, deadline, self._max_bytes)
            except (TimeoutError, OSError, RetrievalError) as exc:
                if isinstance(exc, RetrievalError):
                    response, error = exc.response, str(exc)
                else:
                    error = "timeout" if isinstance(exc, TimeoutError) else "transport_error"
            metadata = {}
            if response:
                for key, value in response.headers:
                    key = key.lower()
                    if key in _SAFE_HEADERS:
                        # Keep redirect diagnostics without recording injected URL credentials.
                        if key == "location":
                            try:
                                target = urlsplit(value)
                                if target.username is not None or target.password is not None:
                                    value = "[redacted]"
                            except ValueError:
                                value = "[invalid]"
                        metadata.setdefault(key, []).append(value)
            attempts.append(dict(url=url, started_at=started,
                                 fetched_at=datetime.now(timezone.utc).isoformat(),
                                 status=response.status if response else None, headers=metadata,
                                 error=error, request_validators=
                                 [key for key in headers if key.startswith("If-")]))
            if error:
                raise RetrievalError(error)
            if response.status in {429, 503}:
                delay = response.header("retry-after")
                if delay and delay.strip().isascii() and delay.strip().isdigit():
                    seconds = int(delay.strip()[:10])
                else:
                    until = timestamp(delay)
                    seconds = datetime.fromisoformat(until).timestamp() - time.time() if until else 0
                if seconds > 0:
                    self.store.defer(policy.source.id, self._mode, seconds)
                    if seconds >= self._timeout:
                        raise RetrievalError(f"http_{response.status}_deferred")
            if response.status not in _REDIRECTS:
                return response, url, sent
            location = response.header("location")
            if not location:
                raise RetrievalError("missing_redirect_location")
            if hop == 3:
                raise RetrievalError("redirect_limit")
            if any(ord(c) < 33 or ord(c) > 126 or c in "\\#" for c in location):
                raise RetrievalError("unsafe_redirect")
            try:
                target = urljoin(url, location)
                approved_host(target, hosts)
                url = target
            except ValueError:
                raise RetrievalError("unsafe_redirect") from None

"""Versioned crawl/query CSV adapters. No network access or inferred deletion.

Original field values survive imports. ``spreadsheet_safe`` is the only map
intended for future spreadsheet exports; HTML consumers must still escape text.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

PROFILE = "screaming_frog_v1"
MAX_BYTES = 2_000_000
MAX_ROWS = 10_000
MAX_CELL = 16_000


def spreadsheet_safe(value):
    """Preserve content while preventing a spreadsheet formula interpretation."""
    if not isinstance(value, str):
        return value
    # A control/space prefix can conceal a formula from simple first-char tests.
    suspicious = value.lstrip(" \t\r\n\ufeff")
    if suspicious.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def _issue(reason, *, row=None, field=None, raw=None, severity="error"):
    item = {"reason": reason, "severity": severity}
    if row is not None:
        item["row"] = row
    if field is not None:
        item["field"] = field
    if raw is not None:
        item["raw"] = raw
    return item


def _csv(text):
    if not isinstance(text, str):
        raise ValueError("CSV input must be text")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("CSV exceeds 2 MB limit")
    if "\x00" in text:
        raise ValueError("CSV contains NUL bytes")
    rows, errors = [], []
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
    try:
        headers = next(reader)
    except StopIteration:
        return [], [], [_issue("CSV is empty")]
    except csv.Error as exc:
        return [], [], [_issue("Malformed CSV: " + str(exc))]
    if not headers or any(not h.strip() for h in headers):
        return headers, [], [_issue("CSV headers must be nonempty")]
    if len(set(h.strip().casefold() for h in headers)) != len(headers):
        return headers, [], [_issue("Duplicate CSV headers", raw=headers)]
    try:
        for number, values in enumerate(reader, start=2):
            if number - 2 >= MAX_ROWS:
                errors.append(_issue("CSV exceeds row limit; remaining input not imported", row=number))
                break
            if len(values) != len(headers):
                errors.append(_issue("Column count does not match headers", row=number, raw=values))
            elif any(len(v) > MAX_CELL for v in values):
                errors.append(_issue("Cell exceeds length limit", row=number, raw=[v[:MAX_CELL] for v in values]))
            elif any(v.strip() for v in values):
                rows.append((number, dict(zip(headers, values))))
    except csv.Error as exc:
        errors.append(_issue("Malformed CSV: " + str(exc), row=reader.line_num))
    return headers, rows, errors


def _key(value):
    return " ".join(value.strip().casefold().replace("_", " ").split())


def _mapped(raw, aliases):
    out = {}
    for key, value in raw.items():
        target = aliases.get(_key(key))
        if target:
            if target in out and out[target] != value:
                raise ValueError("Multiple columns disagree for " + target)
            out[target] = value
    return out


def url_identity(value):
    """Normalize transport identity without dropping query parameters/variants."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL is required")
    value = value.strip()
    if any(ord(ch) < 32 for ch in value) or "\\" in value:
        raise ValueError("URL contains control characters or backslash")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Absolute HTTP(S) URL required")
    if parts.username or parts.password:
        raise ValueError("URL credentials are forbidden")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    host = parts.hostname.lower()
    if any(ch.isspace() for ch in host):
        raise ValueError("Invalid hostname")
    host = "[" + host + "]" if ":" in host else host
    scheme = parts.scheme.lower()
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host += ":" + str(port)
    # Fragment is a document location, not another crawl request. Original URL
    # stays on every record so this reconciliation is visible.
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


CRAWL_ALIASES = {
    "address": "url", "url": "url", "status code": "status",
    "http status": "status", "status": "status", "status_code": "status",
    "canonical link element 1": "canonical", "canonical": "canonical",
    "canonical url": "canonical", "meta robots 1": "meta_robots",
    "meta robots": "meta_robots", "x-robots-tag 1": "x_robots_tag",
    "x-robots-tag": "x_robots_tag", "indexability": "indexability",
    "title 1": "title", "title": "title", "h1-1": "h1", "h1": "h1",
    "crawl depth": "crawl_depth", "inlinks": "inlinks", "content type": "content_type",
    "redirect url": "redirect_url", "redirect uri": "redirect_url",
}


def _metadata(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Crawl metadata must be an object")
    out = dict(metadata)
    errors = []
    out.setdefault("profile", PROFILE)
    if out["profile"] != PROFILE:
        errors.append(_issue("Unsupported crawl profile", field="profile", raw=out["profile"]))
    for field in ("captured_at", "tool_version", "configuration_hash", "scope", "rendering_mode", "complete", "exclusions"):
        out.setdefault(field, None)
        if out[field] is None:
            errors.append(_issue("Missing crawl provenance", field=field))
        elif field not in {"complete", "exclusions"} and not out[field]:
            errors.append(_issue("Empty crawl provenance", field=field))
    scope = out["scope"]
    if scope is not None:
        try:
            if not isinstance(scope, list) or not scope:
                raise ValueError()
            for url in scope:
                url_identity(url)
        except ValueError:
            errors.append(_issue("scope must be a nonempty list of HTTP(S) URLs", field="scope"))
    if out["captured_at"] is not None:
        try:
            stamp = datetime.fromisoformat(str(out["captured_at"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("timezone required")
        except (TypeError, ValueError):
            errors.append(_issue("captured_at must be an ISO timestamp with timezone", field="captured_at"))
    if out["complete"] is not None and type(out["complete"]) is not bool:
        errors.append(_issue("complete must be a boolean", field="complete"))
    if out["rendering_mode"] is not None and out["rendering_mode"] not in {"raw", "rendered", "javascript", "text", "html"}:
        errors.append(_issue("Unsupported rendering mode", field="rendering_mode"))
    return out, errors


def import_crawl(csv_text: str, metadata: dict) -> dict:
    """Import supported Screaming Frog fields; retain errors and source bytes hash."""
    meta, errors = _metadata(metadata)
    headers, source_rows, csv_errors = _csv(csv_text)
    errors.extend(csv_errors)
    # Classify against the profile, not per-row mappings: Status reason text
    # remains recognized when Status Code/HTTP Status supplies the numeric code.
    for header in headers:
        if _key(header) not in CRAWL_ALIASES:
            errors.append(_issue(
                "Unsupported crawl column retained only in raw data; values are not validated or compared",
                field=header, severity="warning"))
    result = {"rows": [], "errors": errors, "metadata": meta,
              "source_hash": hashlib.sha256(csv_text.encode("utf-8")).hexdigest()}
    if not any(CRAWL_ALIASES.get(_key(h)) == "url" for h in headers):
        errors.append(_issue("Required URL/Address column missing", field="url"))
        return result
    seen = set()
    for number, raw in source_rows:
        try:
            # Actual SEO Spider exports commonly contain both numeric Status
            # Code and a human-readable Status (e.g. OK). Preserve the latter
            # in raw data instead of treating it as a contradictory code.
            aliases = CRAWL_ALIASES
            if any(_key(key) in {"status code", "http status"} for key in raw):
                aliases = {key: value for key, value in CRAWL_ALIASES.items() if key != "status"}
            row = _mapped(raw, aliases)
            identity = url_identity(row.get("url", ""))
            if identity in seen:
                raise ValueError("Duplicate URL identity")
            status = row.get("status", "").strip()
            if not status.isdigit() or not 100 <= int(status) <= 599:
                raise ValueError("Valid HTTP status code required; failed fetches remain import errors")
            row["status"] = int(status)
            row["identity"] = identity
            row["source_row"] = number
            row["raw"] = raw
            row["spreadsheet_safe"] = {k: spreadsheet_safe(v) for k, v in raw.items()}
            seen.add(identity)
            result["rows"].append(row)
        except ValueError as exc:
            errors.append(_issue(str(exc), row=number, raw=raw))
    return result


def compare_crawls(before: dict, after: dict) -> dict:
    """Only produce a comparable diff when both datasets establish coverage."""
    limitations = []
    if any(e.get("severity", "error") == "error" for data in (before, after) for e in data.get("errors", [])):
        limitations.append("Import errors mean complete comparable coverage is not established")
    left, right = before.get("metadata", {}), after.get("metadata", {})
    for field in ("profile", "scope", "rendering_mode", "configuration_hash", "exclusions", "tool_version"):
        if left.get(field) is None or right.get(field) is None or left.get(field) != right.get(field):
            limitations.append("Missing or incompatible " + field)
    if left.get("complete") is not True or right.get("complete") is not True:
        limitations.append("At least one crawl is incomplete or completeness is unknown")
    try:
        a = datetime.fromisoformat(str(left.get("captured_at")).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(right.get("captured_at")).replace("Z", "+00:00"))
        if a.tzinfo is None or b.tzinfo is None or b <= a:
            limitations.append("After crawl must have a later timezone-aware capture time")
    except (TypeError, ValueError):
        limitations.append("Capture chronology is unverified")
    comparable = not limitations
    changes = []
    old = {r["identity"]: r for r in before.get("rows", [])}
    new = {r["identity"]: r for r in after.get("rows", [])}
    fields = ("status", "canonical", "meta_robots", "x_robots_tag", "indexability", "title", "h1", "crawl_depth", "inlinks", "redirect_url")
    for identity in sorted(old.keys() | new.keys()):
        if identity not in new:
            changes.append({"url": old[identity]["url"], "kind": "missing_from_after",
                            "interpretation": "Not observed in after crawl; deletion is not established"})
        elif identity not in old:
            changes.append({"url": new[identity]["url"], "kind": "newly_observed",
                            "interpretation": "Newly observed in dataset; publication date is not established"})
        else:
            for field in fields:
                if old[identity].get(field) != new[identity].get(field):
                    changes.append({"url": new[identity]["url"], "kind": "field_changed", "field": field,
                                    "before": old[identity].get(field), "after": new[identity].get(field)})
    return {"changes": changes, "limitations": limitations, "comparable": comparable,
            "before_source_hash": before.get("source_hash"), "after_source_hash": after.get("source_hash"),
            "coverage": {"before": len(old), "after": len(new)},
            "acceptance_eligible": comparable}


QUERY_ALIASES = {"query": "query", "keyword": "query", "origin": "origin", "source type": "origin",
                 "source": "source_ref", "source ref": "source_ref", "source url": "source_ref",
                 "country": "country", "language": "language", "observed at": "observed_at",
                 "date": "observed_at", "intent": "intent", "target url": "target_url"}


def import_queries(csv_text: str) -> dict:
    headers, source_rows, errors = _csv(csv_text)
    rows, seen = [], set()
    source_hash = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    if not any(QUERY_ALIASES.get(_key(h)) == "query" for h in headers):
        errors.append(_issue("Required query/keyword column missing", field="query"))
        return {"rows": rows, "errors": errors, "source_hash": source_hash}
    unsupported = [h for h in headers if _key(h) not in QUERY_ALIASES]
    if unsupported:
        errors.append(_issue("Unsupported columns retained only in raw data: " + ", ".join(unsupported), severity="warning"))
    for number, raw in source_rows:
        try:
            row = _mapped(raw, QUERY_ALIASES)
            if not row.get("query", "").strip():
                raise ValueError("Query is required")
            if row.get("origin") not in {"first_party", "vendor", "hypothesis"}:
                raise ValueError("Origin must be first_party, vendor or hypothesis")
            if not row.get("source_ref", "").strip():
                raise ValueError("Every query requires source provenance, including hypotheses")
            for field in ("country", "language", "observed_at", "intent", "target_url"):
                row[field] = row.get(field) or None
            if row["observed_at"]:
                try:
                    datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("Invalid query observation date") from exc
            if row["target_url"]:
                url_identity(row["target_url"])
            identity = json.dumps([row["query"], row["origin"], row["source_ref"], row["country"], row["language"], row["observed_at"]])
            if identity in seen:
                raise ValueError("Duplicate query/provenance record")
            seen.add(identity)
            row.update(source_row=number, raw=raw,
                       spreadsheet_safe={k: spreadsheet_safe(v) for k, v in raw.items()},
                       limitations=(["Generated hypothesis; actual retrieval query is not established"] if row["origin"] == "hypothesis" else []) +
                                   (["Observation date unknown"] if not row["observed_at"] else []))
            rows.append(row)
        except ValueError as exc:
            errors.append(_issue(str(exc), row=number, raw=raw))
    return {"rows": rows, "errors": errors,
            "source_hash": hashlib.sha256(csv_text.encode("utf-8")).hexdigest()}

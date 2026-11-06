"""S11 offline synthetic experiments v1. Pure functions; no network/store/exporter.

Lexical support means token coverage only, pending human review. It does not
establish truth, semantic entailment, rankings, hidden queries or causal traffic.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

VERSION = "s11-offline-v1"
SCOPE = ("market", "language", "period_start", "period_end", "timezone")
ORIGINS = {"first_party": "observed", "vendor": "vendor", "hypothesis": "hypothesis"}
STOP = frozenset("a an the is are of for with and to in un une le la les de en las tienen have has".split())
METRICS = {
    "ahrefs": ("vendor", {"vendor_estimated_volume": "estimated_queries_per_month"}),
    "gsc": ("observed", {"clicks": "clicks", "impressions": "impressions"}),
    "bing": ("observed", {"clicks": "clicks", "impressions": "impressions"}),
    "ga4_referrals": ("observed", {"referral_sessions": "sessions"}),
    "ai_visibility": ("observed", {"completed_responses": "responses", "citation_present_responses": "responses"}),
}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise ValueError("nonempty bounded text required")
    return value


def _refs(record, sources):
    refs = record.get("source_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in sources for r in refs):
        raise ValueError("unresolved source reference")
    if record.get("synthetic") is not True:
        raise ValueError("synthetic fixture required")


def _scope(value):
    if not isinstance(value, dict) or any(k not in value for k in SCOPE):
        raise ValueError("explicit scope required")
    for k in SCOPE:
        if value[k] is not None:
            _text(value[k])
    try:
        start = date.fromisoformat(value["period_start"]) if value["period_start"] else None
        end = date.fromisoformat(value["period_end"]) if value["period_end"] else None
        if start and end and start > end:
            raise ValueError("reversed period")
        if value["timezone"]:
            ZoneInfo(value["timezone"])
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid scope") from exc
    return all(value[k] is not None for k in SCOPE)


def _query(query, sources):
    _refs(query, sources)
    _text(query.get("id"))
    _text(query.get("text"))
    origin = query.get("origin")
    if origin is None:
        if query.get("subtype") != "platform_returned_sample" or query.get("evidence_class") != "observed":
            raise ValueError("platform sample subtype/class required")
    elif origin not in ORIGINS or ORIGINS[origin] != query.get("evidence_class"):
        raise ValueError("origin/class promotion forbidden")
    stamp = datetime.fromisoformat(query["recorded_at"])
    if stamp.tzinfo is None:
        raise ValueError("capture timestamp must include timezone")
    if not isinstance(query.get("metrics"), list):
        raise ValueError("explicit metrics list required")
    return _scope(query.get("scope"))


def _tokens(text):
    return set(re.findall(r"[^\W_]+", text.casefold(), re.UNICODE)) - STOP


def map_query(query, passages, sources):
    """Exact scope + conservative lexical sentence matching, Unicode offsets.

    Sources is a frozen registry mapping IDs to local evidence pointers. The
    caller owns source bytes; passage bytes are checked here before ranking.
    """
    known = _query(query, sources)
    if not isinstance(passages, list) or len(passages) > 100:
        raise ValueError("at most 100 passages")
    seen = set()
    compatible = []
    for p in passages:
        _refs(p, sources)
        _text(p.get("id"))
        if p["id"] in seen:
            raise ValueError("duplicate passage ID")
        seen.add(p["id"])
        if digest(_text(p.get("text"))) != p.get("sha256"):
            raise ValueError("passage hash mismatch")
        pk = _scope(p.get("scope"))
        if known and pk and query["scope"] == p["scope"]:
            compatible.append(p)
    out = dict(query_id=query["id"], passage_id=None, passage_sha256=None,
               span_start=None, span_end=None, quote=None, scope=deepcopy(query["scope"]),
               verdict="no_passage", source_refs=list(query["source_refs"]),
               evidence_class=query["evidence_class"], origin=query["origin"],
               subtype=query["subtype"], synthetic=True, review_state="suggestion_pending",
               reason="No fully matching lexical passage; review content coverage, no ranking claim.")
    if not known:
        out.update(verdict="unknown_scope", reason="Unknown query scope; review required.")
        return out
    if passages and not compatible:
        out.update(verdict="scope_mismatch", reason="No exact known compatible passage scope.")
        return out
    tokens = _tokens(query["text"])
    candidates = []
    for p in compatible:
        for span in re.finditer(r"[^.!?\n]+[.!?]?", p["text"]):
            raw = span.group()
            quote = raw.strip()
            start = span.start() + len(raw) - len(raw.lstrip())
            if tokens and tokens <= _tokens(quote):
                candidates.append((len(_tokens(quote) - tokens), p["id"], start, quote, p))
    if candidates:
        _, _, start, quote, p = min(candidates, key=lambda c: c[:3])
        out.update(verdict="supported", passage_id=p["id"], passage_sha256=p["sha256"],
                   span_start=start, span_end=start + len(quote), quote=quote,
                   source_refs=list(dict.fromkeys(out["source_refs"] + p["source_refs"])),
                   reason="Full lexical coverage in exact scoped snapshot; semantic review pending.")
    return out


def fanout(parent, children, sources):
    """Accept up to three explicitly authored hypotheses; never inherit metrics."""
    _query(parent, sources)
    if not isinstance(children, list) or len(children) > 3:
        raise ValueError("fanout cap is three")
    result, seen, ids = [], set(), set()
    for child in children:
        if set(child) - {"id", "parent_query_id", "text", "rationale", "source_refs", "relevant"}:
            raise ValueError("unsupported hypothesis input fields")
        if child.get("parent_query_id") != parent["id"]:
            raise ValueError("parent link mismatch")
        _text(child.get("id"))
        text = _text(child.get("text"))
        norm = " ".join(re.findall(r"[^\W_]+", text.casefold()))
        if norm in seen or child["id"] in ids or norm == " ".join(re.findall(r"[^\W_]+", parent["text"].casefold())):
            raise ValueError("duplicate hypothesis")
        seen.add(norm)
        ids.add(child["id"])
        _text(child.get("rationale"))
        out = dict(id=child["id"], text=text, parent_query_id=parent["id"],
                   rationale=child["rationale"], source_refs=child.get("source_refs"),
                   scope=deepcopy(parent["scope"]), evidence_class="hypothesis", origin="hypothesis",
                   subtype="manual_variant", synthetic=True, metrics=[], recorded_at=parent["recorded_at"])
        _query(out, sources)
        out["source_refs"] = list(dict.fromkeys(parent["source_refs"] + out["source_refs"]))
        result.append(out)
    return result


def normalize_adapter(record, sources):
    """Normalize bounded synthetic envelopes; malformed input is a rejection.

    Validate containers before accessing fields. Error receipts never reflect raw
    input objects and cannot assert coverage completeness after a rejection.
    """
    record_id = None
    if isinstance(record, dict):
        candidate = record.get("id", record.get("adapter_id"))
        if isinstance(candidate, str) and len(candidate) <= 128:
            record_id = candidate

    def rejected(reason):
        return dict(accepted=[], rejected=[{"record_id": record_id, "reason": reason[:256]}],
                    provenance_gaps=[], coverage={"complete": False}, checkpoint=None)

    try:
        if not isinstance(record, dict):
            raise ValueError("adapter envelope must be an object")
        if not isinstance(sources, dict) or not sources or len(sources) > 1000:
            raise ValueError("bounded source registry object required")
        for key, pointer in sources.items():
            _text(key)
            _text(pointer)
        refs = record.get("source_refs")
        if not isinstance(refs, list) or not refs or len(refs) > 100:
            raise ValueError("bounded source reference list required")
        for ref in refs:
            _text(ref)
        _refs(record, sources)
        if "id" in record:
            _text(record["id"])
        known = _scope(record.get("scope"))
        adapter_id = record.get("adapter_id")
        if not isinstance(adapter_id, str) or adapter_id not in METRICS:
            raise ValueError("unknown adapter")
        cls, allowed = METRICS[adapter_id]
        if record.get("evidence_class") != cls:
            raise ValueError("provider class promotion")
        metrics = record.get("metrics")
        if not isinstance(metrics, list) or len(metrics) > 100:
            raise ValueError("bounded metrics list required")
        seen = set()
        for metric in metrics:
            if not isinstance(metric, dict):
                raise ValueError("metric must be an object")
            kind = metric.get("kind")
            if not isinstance(kind, str) or kind not in allowed or metric.get("unit") != allowed[kind]:
                raise ValueError("unsupported metric kind or unit")
            value = metric.get("value")
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or kind in seen:
                raise ValueError("invalid or duplicate metric")
            seen.add(kind)
        coverage = record.get("coverage")
        if not isinstance(coverage, dict):
            raise ValueError("coverage must be an object")
        returned, total = coverage.get("returned"), coverage.get("total_eligible")
        if type(returned) is not int or returned < 0 or (total is not None and (type(total) is not int or total < returned)):
            raise ValueError("invalid coverage counts")
        if coverage.get("truncated") is not None and type(coverage["truncated"]) is not bool:
            raise ValueError("invalid truncation")
        if type(coverage.get("complete")) is not bool:
            raise ValueError("coverage complete must be boolean")
        checkpoint = record.get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise ValueError("fixture checkpoint must be an object")
        _text(checkpoint.get("fixture_id"))
        _text(checkpoint.get("resume_state"))
        if "next_page" not in checkpoint:
            raise ValueError("explicit checkpoint next_page required")
        if checkpoint["next_page"] is not None:
            _text(checkpoint["next_page"])
        # Check the whole preserved envelope, including optional extra fields,
        # before hashing/copying it. Reject non-JSON, nonfinite, cyclic or huge data.
        if len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2_000_000:
            raise ValueError("adapter envelope exceeds byte bound")
        complete = (known and coverage["complete"] is True and total is not None and returned == total
                    and coverage.get("truncated") is False and checkpoint["next_page"] is None)
        out = deepcopy(record)
        out["coverage"]["complete"] = bool(complete)
        out["input_sha256"] = fingerprint(record)
        out["query_availability"] = "unknown_unreturned_queries"
        out["record_kind"] = "referral_sessions" if adapter_id == "ga4_referrals" else "source_observation"
        return dict(accepted=[out], rejected=[], provenance_gaps=[] if known else ["unknown_scope"],
                    coverage=out["coverage"], checkpoint=deepcopy(checkpoint))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        # Stable bounded messages, rather than raw values or exception strings.
        return rejected("Malformed or unsupported synthetic adapter envelope")


def reconcile_ai(observations, queries, scope, sources, snapshots):
    """Count citations only over completed compatible fixed synthetic responses.

    Missing/hidden queries stay unknown; retries deduplicate by exact response ID.
    Query samples never infer referral sessions, conversions or revenue.
    """
    if not _scope(scope):
        raise ValueError("known denominator scope required")
    if len(observations) > 100 or len(queries) > 100:
        raise ValueError("bounded observations/queries required")
    index = {}
    for q in queries:
        _query(q, sources)
        if q["id"] in index:
            raise ValueError("duplicate query ID")
        index[q["id"]] = q
    unique, excluded, duplicates, rows = {}, [], 0, []
    experiment = None
    completed = failed = cited = 0
    for obs in observations:
        _refs(obs, sources)
        _scope(obs.get("scope"))
        _text(obs.get("id"))
        if any(k in obs for k in ("hidden_queries", "revenue", "conversions", "causal_revenue")):
            raise ValueError("unsupported hidden/causal claim")
        if obs["id"] in unique:
            if unique[obs["id"]] != fingerprint(obs):
                raise ValueError("conflicting duplicate observation")
            duplicates += 1
            continue
        unique[obs["id"]] = fingerprint(obs)
        if obs["scope"] != scope:
            excluded.append(obs["id"])
            continue
        for key in ("prompt_set_id", "model_id", "settings_ref"):
            _text(obs.get(key))
        config = tuple(obs[k] for k in ("prompt_set_id", "model_id", "settings_ref"))
        if experiment is not None and config != experiment:
            raise ValueError("incompatible AI experiment configuration")
        experiment = config
        status = obs.get("status")
        if status not in ("completed", "failed", "pending"):
            raise ValueError("invalid response status")
        citations = obs.get("citations", [])
        if not isinstance(citations, list):
            raise ValueError("citation list required")
        for citation in citations:
            _refs({**citation, "synthetic": True}, sources)
            url = urlsplit(_text(citation.get("url")))
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                raise ValueError("invalid citation URL")
        returned = obs.get("returned_query_ids")
        if returned is not None and not isinstance(returned, list):
            raise ValueError("returned query sample list required")
        for qid in returned or []:
            if qid not in index or index[qid]["subtype"] != "platform_returned_sample" or index[qid]["scope"] != scope:
                raise ValueError("unresolved/incompatible returned query")
        if status == "completed":
            ref = _text(obs.get("answer_snapshot_ref"))
            if ref not in snapshots:
                raise ValueError("unresolved answer snapshot")
            snapshot = snapshots[ref]
            _refs(snapshot, sources)
            if digest(_text(snapshot.get("text"))) != snapshot.get("sha256"):
                raise ValueError("answer snapshot hash mismatch")
            completed += 1
            cited += bool(citations)
        failed += status == "failed"
        rows.append(dict(id=obs["id"], status=status, returned_query_ids=deepcopy(returned),
                         query_coverage="unknown_hidden_queries_unavailable", synthetic=True))
    return dict(synthetic=True, total_scoped=len(rows), completed=completed, failed=failed,
                pending=len(rows)-completed-failed, citation_present=cited,
                citation_present_fraction=cited/completed if completed else None,
                excluded_scope_ids=excluded, duplicate_responses=duplicates, rows=rows,
                referral_sessions=None, conversions=None, revenue=None,
                hidden_queries="unavailable", delivery="local_synthetic_only")


EVENT_KEYS = frozenset(("event_id", "run_id", "case_id", "stage", "contract_version", "fixture_id", "status", "duration_ms", "input_sha256", "scores", "cost_usd", "tokens", "synthetic"))
SCORES = frozenset(("reference_integrity", "scope_preserved", "abstention_correct"))


def sanitize_event(event, *, run_id, case_id, input_sha256):
    """Strict typed allowlist and exact binding; raw payload fields rejected.

    IDs must be opaque generated hex, not human labels. No external exporter.
    """
    if not isinstance(event, dict) or set(event) != EVENT_KEYS:
        raise ValueError("event allowlist mismatch")
    for key in ("event_id", "run_id", "case_id", "fixture_id"):
        if not isinstance(event[key], str) or not re.fullmatch(r"[0-9a-f]{32}", event[key]):
            raise ValueError("opaque identifier required")
    if event["run_id"] != run_id or event["case_id"] != case_id or event["input_sha256"] != input_sha256:
        raise ValueError("event binding mismatch")
    if not isinstance(input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise ValueError("input hash required")
    if event["contract_version"] != VERSION or event["stage"] not in ("E1", "E2", "E3", "E4", "E5") or event["status"] not in ("passed", "failed") or event["synthetic"] is not True:
        raise ValueError("fixed event metadata required")
    if not isinstance(event["scores"], dict) or set(event["scores"]) != SCORES or any(type(v) is not bool for v in event["scores"].values()):
        raise ValueError("typed score allowlist required")
    d = event["duration_ms"]
    if d is not None and (type(d) not in (int, float) or not math.isfinite(d) or d < 0):
        raise ValueError("invalid duration")
    if event["cost_usd"] is not None or event["tokens"] is not None:
        raise ValueError("unmeasured fixture cost/tokens must remain null")
    return deepcopy(event)

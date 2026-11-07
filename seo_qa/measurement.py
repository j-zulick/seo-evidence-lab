"""Normalize saved Search Analytics and CrUX responses without remote access."""
import argparse
import json
import math
from datetime import date
from pathlib import Path

from .competitor import ROOT, digest, url_key, read_json


def window(start, end):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError('Reversed reporting window')
    return [start, end]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected a finite nonnegative observation')
    return value


def gsc(payload):
    req = payload['request']
    dims = req['dimensions']
    if not isinstance(dims, list) or any(not isinstance(dim, str) or not dim for dim in dims):
        raise ValueError('Dimension names must be a list of nonempty strings')
    if 'page' not in dims or len(dims) != len(set(dims)):
        raise ValueError('Unique dimensions including page required')
    period = window(req['startDate'], req['endDate'])
    source_rows = payload['response'].get('rows', [])
    if not isinstance(source_rows, list) or len(source_rows) > 2000:
        raise ValueError('Expected at most 2000 measurement rows')
    rows, seen = [], set()
    for row in source_rows:
        keys = row.get('keys') if isinstance(row, dict) else None
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise ValueError('Dimension keys must be a list of strings')
        fields = dict(zip(dims, keys, strict=True))
        fields['page'] = url_key(fields['page'])
        if 'date' in fields:
            observed = date.fromisoformat(fields['date'])
            if not date.fromisoformat(period[0]) <= observed <= date.fromisoformat(period[1]):
                raise ValueError('Observation date is outside the reporting window')
        identity = tuple(fields[dim] for dim in dims)
        if identity in seen:
            raise ValueError('Duplicate dimension-key observation')
        seen.add(identity)
        for key in ('clicks', 'impressions', 'position'):
            fields[key] = number(row[key])
        rows.append(fields)
    return dict(source='Search Console', property=req['property'], period=period, rows=rows,
                coverage='reported rows' if rows else 'unavailable', source_hash=digest(payload))


def crux(payload):
    if payload.get('error', {}).get('status') == 'NOT_FOUND':
        return dict(source='CrUX', coverage='unavailable', request=payload.get('request'), source_hash=digest(payload))
    record = payload['record']
    key = record['key']
    scopes = [s for s in ('url', 'origin') if s in key]
    if len(scopes) != 1:
        raise ValueError('One CrUX scope required')
    # A retained request must identify this response. Explicit API normalization
    # can change a URL; require both ends of that mapping rather than guessing.
    request = payload.get('request')
    if request is not None:
        if not isinstance(request, dict):
            raise ValueError('CrUX request must be an object')
        requested_scopes = [scope for scope in ('url', 'origin') if scope in request]
        if requested_scopes != scopes:
            raise ValueError('CrUX request and response scope differ')
        scope = scopes[0]
        requested_target, returned_target = url_key(request[scope]), url_key(key[scope])
        if requested_target != returned_target:
            normalization = payload.get('urlNormalizationDetails')
            if (scope != 'url' or not isinstance(normalization, dict)
                    or url_key(normalization.get('originalUrl')) != requested_target
                    or url_key(normalization.get('normalizedUrl')) != returned_target):
                raise ValueError('CrUX request and response target differ')
        if request.get('formFactor', 'ALL') != key.get('formFactor', 'ALL'):
            raise ValueError('CrUX request and response device differ')
    dates = record['collectionPeriod']
    period = window(*(date(**dates[k]).isoformat() for k in ('firstDate', 'lastDate')))
    metrics = {}
    for name in ('largest_contentful_paint', 'interaction_to_next_paint', 'cumulative_layout_shift'):
        raw = record['metrics'].get(name, {}).get('percentiles', {}).get('p75')
        if raw is None:
            metrics[name] = None
        else:
            try:
                # CrUX supplies decimal metrics such as CLS as numeric strings.
                metrics[name] = number(raw if isinstance(raw, bool) else float(raw))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('Invalid CrUX percentile: ' + name) from exc
    return dict(source='CrUX', scope=scopes[0], target=url_key(key[scopes[0]]), device=key.get('formFactor','ALL'),
                period=period, p75=metrics, coverage='reported metrics' if any(v is not None for v in metrics.values()) else 'unavailable',
                source_hash=digest(payload))


def comparison_allowed(left, right):
    """Require a like-for-like series before calculating a period-to-period delta."""
    identity = ('source','property','scope','target','device','country','query','metric','unit')
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if any(left.get(k) != right.get(k) for k in identity) or not all(left.get(k) for k in ('source','scope','target','metric','unit')):
        return False
    periods = []
    try:
        for row in (left, right):
            number(row['value'])
            period = row['period']
            if not isinstance(period, (list, tuple)) or len(period) != 2:
                return False
            start, end = map(date.fromisoformat, period)
            if start > end:
                return False
            periods.append((start, end))
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
    (a, b), (c, d) = periods
    return b < c and b - a == d - c


def fixture_view():
    base = ROOT/'fixtures/measurement'
    return {name:func(json.loads((base/(name+'.json')).read_text())) for name,func in [('gsc',gsc),('crux',crux)]}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source',choices=('gsc','crux'))
    p.add_argument('file',type=Path)
    args=p.parse_args()
    print(json.dumps({'gsc':gsc,'crux':crux}[args.source](read_json(args.file)),indent=2))

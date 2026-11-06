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
    if 'page' not in dims or len(dims) != len(set(dims)):
        raise ValueError('Unique dimensions including page required')
    period = window(req['startDate'], req['endDate'])
    if len(payload['response'].get('rows',[]))>2000:
        raise ValueError('Row limit exceeded')
    rows = []
    for row in payload['response'].get('rows', []):
        fields = dict(zip(dims, row['keys'], strict=True))
        fields['page'] = url_key(fields['page'])
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
    dates = record['collectionPeriod']
    period = window(*(date(**dates[k]).isoformat() for k in ('firstDate', 'lastDate')))
    metrics = {}
    for name in ('largest_contentful_paint', 'interaction_to_next_paint', 'cumulative_layout_shift'):
        raw = record['metrics'].get(name, {}).get('percentiles', {}).get('p75')
        metrics[name] = None if raw is None else number(raw if isinstance(raw,bool) else float(raw))
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

"""Versioned saved-export contract; reads local JSON and never calls a provider.

The envelope is this application's adapter format, not a claimed vendor API.
"""
import argparse
from datetime import date, datetime
import json
from pathlib import Path

from .competitor import digest, read_json, url_key
from .measurement import gsc, crux, number, window

SCHEMA = 'seo-qa/saved-provider/v1'
NORMALIZER = 'provider-normalizer/v1'


def normalize(envelope):
    """Retain exact source identity while producing comparable scalar series."""
    if not isinstance(envelope, dict) or envelope.get('schema') != SCHEMA:
        raise ValueError('Unsupported saved-export schema')
    if type(envelope.get('synthetic')) is not bool:
        raise ValueError('Explicit synthetic provenance required')
    captured = datetime.fromisoformat(envelope['exported_at'])
    if captured.tzinfo is None:
        raise ValueError('Export time requires a timezone')
    provider = envelope['provider']
    payload = envelope['payload']
    if not isinstance(payload, dict):
        raise ValueError('Payload must be an object')
    common = dict(schema=SCHEMA, normalizer=NORMALIZER, provider=provider,
                  synthetic=envelope['synthetic'], exported_at=captured.isoformat(),
                  source_hash=digest(envelope), payload_hash=digest(payload))
    series = []
    exported_day = captured.date().isoformat()

    def append(identity, period, metric, unit, value):
        if value is None:
            return
        if period[1] > exported_day:
            raise ValueError('Observation window ends after export time')
        series.append(dict(identity=identity, period=period, metric=metric,
                           unit=unit, value=number(value), **common))

    if provider == 'gsc':
        parsed = gsc(payload)
        request = payload['request']
        if not isinstance(request.get('property'), str) or not request['property'].strip():
            raise ValueError('GSC property identity required')
        # Search type, filters, aggregation and freshness are part of a series,
        # even when the row has no explicit dimensions for them.
        context = {key: request.get(key, default) for key, default in
                   [('type', request.get('searchType', 'web')), ('dataState', 'final'),
                    ('aggregationType', 'auto'), ('dimensionFilterGroups', [])]}
        if any(not isinstance(context[key], str) or not context[key] for key in ('type', 'dataState', 'aggregationType')) or not isinstance(context['dimensionFilterGroups'], list):
            raise ValueError('Invalid GSC request context')
        if 'searchType' in request and 'type' in request and request['searchType'] != request['type']:
            raise ValueError('Conflicting GSC search types')
        for row in parsed['rows']:
            if any(type(row[key]) is not int for key in ('clicks', 'impressions')):
                raise ValueError('GSC counts must be integers')
            if row['clicks'] > row['impressions']:
                raise ValueError('Clicks cannot exceed impressions')
            identity = dict(property=parsed['property'], scope='url', target=row['page'],
                            dimensions={key: row[key] for key in request['dimensions'] if key != 'date'},
                            request_context=context)
            period = [row['date'], row['date']] if 'date' in row else parsed['period']
            for metric, unit in [('clicks', 'count'), ('impressions', 'count'), ('position', 'rank')]:
                append(identity, period, metric, unit, row[metric])
    elif provider == 'crux':
        parsed = crux(payload)
        if parsed['coverage'] != 'unavailable':
            identity = {key: parsed[key] for key in ('scope', 'target', 'device')}
            for metric, value in parsed['p75'].items():
                append(identity, parsed['period'], metric,
                       'ratio' if metric == 'cumulative_layout_shift' else 'ms', value)
    elif provider == 'ahrefs-saved-top10':
        # Explicit bounded application export. Do not imply compatibility with
        # unknown Ahrefs API response generations or account entitlements.
        request = payload['request']
        if request.get('mode') != 'exact' or request.get('history_grouping') != 'daily':
            raise ValueError('Only exact daily Ahrefs saved observations supported')
        target = url_key(request['target'])
        country = request['country']
        if not isinstance(country, str) or len(country) != 2 or not country.isalpha():
            raise ValueError('Two-letter country required')
        period = window(request['date_from'], request['date_to'])
        rows = payload['keywords']
        if not isinstance(rows, list) or len(rows) > 2000:
            raise ValueError('Expected at most 2000 saved observations')
        seen = set()
        for row in rows:
            observed = date.fromisoformat(row['date']).isoformat()
            if not period[0] <= observed <= period[1] or observed in seen:
                raise ValueError('Duplicate or out-of-window observation')
            seen.add(observed)
            counts = [row[key] for key in ('top3', 'top4_10')]
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError('Keyword counts must be nonnegative integers')
            append(dict(scope='url', target=target, country=country.lower(), mode='exact',
                        history_grouping='daily'), [observed, observed], 'top10_keywords', 'count', sum(counts))
    else:
        raise ValueError('Unsupported provider adapter')
    return dict(**common, coverage='reported observations' if series else 'unavailable', series=series)


def compare(left, right):
    """Calculate a delta only for matching, versioned, nonoverlapping windows."""
    missing = dict(status='unavailable', delta=None)
    if not isinstance(left, dict) or not isinstance(right, dict):
        return missing
    if left.get('value') is None or right.get('value') is None:
        return missing
    fields = ('schema', 'normalizer', 'provider', 'synthetic', 'identity', 'metric', 'unit')
    if any(key not in left or key not in right or left[key] != right[key] for key in fields):
        return dict(status='incompatible identity', delta=None)
    try:
        if left['schema'] != SCHEMA or left['normalizer'] != NORMALIZER:
            raise ValueError('Unknown normalization generation')
        for row in (left, right):
            if type(row['synthetic']) is not bool or not isinstance(row['identity'], dict) or not row['identity'] or not row['metric'] or not row['unit']:
                raise ValueError('Incomplete comparison identity')
            number(row['value'])
            if not isinstance(row['period'], list) or len(row['period']) != 2:
                raise ValueError('Two reporting dates required')
            window(*row['period'])
            for key in ('source_hash', 'payload_hash'):
                value = row[key]
                if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                    raise ValueError('Retained source hash required')
        a, b = map(date.fromisoformat, left['period'])
        c, d = map(date.fromisoformat, right['period'])
        if b >= c or b - a != d - c:
            return dict(status='incompatible windows', delta=None)
    except (ValueError, TypeError, KeyError, OverflowError):
        return dict(status='invalid observation', delta=None)
    return dict(status='comparable', before=left['value'], after=right['value'],
                delta=right['value'] - left['value'],
                sources=[left['source_hash'], right['source_hash']],
                interpretation='Observed change; causal impact is unmeasured')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    args = parser.parse_args()
    print(json.dumps(normalize(read_json(args.export)), indent=2))


if __name__ == '__main__':
    main()

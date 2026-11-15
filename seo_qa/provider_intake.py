"""Drop-folder intake for real saved provider exports (offline, no provider calls).

Layout created by `init`:

    <drop>/inbox/gsc/                 saved GSC Search Analytics JSON (request + response)
    <drop>/inbox/crux/                saved CrUX record JSON
    <drop>/inbox/ahrefs-saved-top10/  this project's saved daily top-10 export
    <drop>/normalized/                written by `run` (never edited by hand)
    <drop>/INTAKE-REPORT.json         written by `run`

Each export `name.json` needs a sidecar `name.meta.json`:
    {"exported_at": "2026-11-14T09:00:00-05:00", "synthetic": false, "note": "..."}
`synthetic` must be declared explicitly. A full `seo-qa/saved-provider/v1`
envelope may be dropped instead of a raw payload; then no sidecar is needed.

`run` never moves, edits or deletes inbox files. It normalizes each export
twice (results must be identical), writes the normalized result named by its
source hash, and lists every comparable pair (same identity and metric,
equal-length nonoverlapping windows). Rejections are reported with a reason;
private payload values never appear in the report.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .competitor import digest
from .provider_contract import NORMALIZER, SCHEMA, compare, normalize

PROVIDERS = ('gsc', 'crux', 'ahrefs-saved-top10')
MAX_BYTES = 20 * 1024 * 1024
README = """Drop saved provider exports here. Nothing in this folder is fetched or uploaded.

inbox/<provider>/<name>.json       the export exactly as saved
inbox/<provider>/<name>.meta.json  {"exported_at": "<ISO time with timezone>", "synthetic": false, "note": ""}

Then run:  .venv/bin/python -m seo_qa.provider_intake run <this folder>
Keep this folder private. Do not add it to a public repository or release.
"""


def init(drop):
    drop = Path(drop)
    for provider in PROVIDERS:
        (drop / 'inbox' / provider).mkdir(parents=True, exist_ok=True)
    (drop / 'normalized').mkdir(exist_ok=True)
    readme = drop / 'README.txt'
    if not readme.exists():
        readme.write_text(README)
    return {'initialized': str(drop), 'providers': list(PROVIDERS)}


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Not a regular file')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('File exceeds 20 MB')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('File is not valid UTF-8 JSON') from None


def envelope_for(provider, path):
    data = _read(path)
    if isinstance(data, dict) and data.get('schema') == SCHEMA:
        if data.get('provider') != provider:
            raise ValueError('Envelope provider does not match its folder')
        return data
    meta_path = path.with_name(path.name[:-len('.json')] + '.meta.json')
    if not meta_path.exists():
        raise ValueError('Missing .meta.json sidecar with exported_at and synthetic')
    meta = _read(meta_path)
    if not isinstance(meta, dict) or set(meta) - {'exported_at', 'synthetic', 'note'}:
        raise ValueError('Sidecar allows only exported_at, synthetic and note')
    if type(meta.get('synthetic')) is not bool:
        raise ValueError('Sidecar must declare synthetic true or false')
    if not isinstance(meta.get('exported_at'), str):
        raise ValueError('Sidecar must give exported_at with a timezone')
    return {'schema': SCHEMA, 'provider': provider, 'synthetic': meta['synthetic'],
            'exported_at': meta['exported_at'], 'payload': data}


def _series_key(row):
    return digest([row['provider'], row['identity'], row['metric'], row['unit'], row['synthetic']])


def run(drop):
    drop = Path(drop)
    inbox = drop / 'inbox'
    if not inbox.is_dir():
        raise ValueError('Run init first: inbox folder not found')
    out = drop / 'normalized'
    out.mkdir(exist_ok=True)
    accepted, rejected, series = [], [], []
    for provider in PROVIDERS:
        folder = inbox / provider
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob('*.json')):
            if path.name.endswith('.meta.json'):
                continue
            label = provider + '/' + path.name
            try:
                envelope = envelope_for(provider, path)
                first, second = normalize(envelope), normalize(json.loads(json.dumps(envelope)))
                if first != second:
                    raise ValueError('Normalization was not reproducible')
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                reason = str(error) if isinstance(error, ValueError) and len(str(error)) < 200 else type(error).__name__
                rejected.append({'file': label, 'reason': reason})
                continue
            target = out / (first['source_hash'] + '.json')
            body = json.dumps(first, indent=2, sort_keys=True)
            if target.exists() and target.read_text() != body:
                rejected.append({'file': label, 'reason': 'Existing normalized result differs'})
                continue
            if not target.exists():
                tmp = target.with_suffix('.tmp')
                tmp.write_text(body)
                os.replace(tmp, target)
            accepted.append({'file': label, 'provider': provider, 'synthetic': first['synthetic'],
                             'source_hash': first['source_hash'], 'coverage': first['coverage'],
                             'observations': len(first['series'])})
            series.extend(first['series'])
    groups = {}
    for row in series:
        groups.setdefault(_series_key(row), []).append(row)
    comparisons = []
    for rows in groups.values():
        rows.sort(key=lambda r: (r['period'][0], r['source_hash']))
        for i, left in enumerate(rows):
            for right in rows[i + 1:]:
                result = compare(left, right)
                if result['status'] == 'comparable':
                    comparisons.append({'provider': left['provider'], 'metric': left['metric'],
                                        'unit': left['unit'], 'target': left['identity'].get('target'),
                                        'before_period': left['period'], 'after_period': right['period'],
                                        **result})
    report = {'normalizer': NORMALIZER, 'accepted': accepted, 'rejected': rejected,
              'real_exports': sum(not a['synthetic'] for a in accepted),
              'comparisons': comparisons,
              'interpretation': 'Observed saved-export values only; no live provider call, no causal claim.'}
    (drop / 'INTAKE-REPORT.json').write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m seo_qa.provider_intake')
    parser.add_argument('command', choices=('init', 'run'))
    parser.add_argument('folder')
    args = parser.parse_args(argv)
    result = init(args.folder) if args.command == 'init' else run(args.folder)
    if args.command == 'run':
        result = {k: result[k] for k in ('accepted', 'rejected', 'real_exports')} | {
            'comparisons': len(result['comparisons'])}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

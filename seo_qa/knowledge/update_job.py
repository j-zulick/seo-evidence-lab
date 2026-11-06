"""Run with python -m seo_qa.knowledge.update_job. No scheduler is installed."""
import argparse
import json
import logging
from pathlib import Path

from .capture import ApprovedSource, Fixtures, Response
from .contracts import Source, sha256
from .operations import UpdateOperations, Settings, SourceLimits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('action', choices=['run', 'tick', 'status'])
    parser.add_argument('--job-id')
    parser.add_argument('--repository', type=Path, help='Read-only published evidence coverage')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.demo:
        url = 'https://publisher.example/text'
        sources = [ApprovedSource(Source(id='src:' + sha256(url.encode()), canonical_url=url,
                   publisher='fixture', authority='official_primary'), 'text', min_interval=0)]
        fixtures = Fixtures({url: [Response(200, (('content-type', 'text/plain'),), b'Pages must have titles.')]})
        ops = UpdateOperations(args.directory, sources, fixtures=fixtures)
    else:
        if args.config is None:
            parser.error('--config or --demo required')
        config = json.loads(args.config.read_text())
        sources = [ApprovedSource(source=Source.model_validate(item['source']),
                   **{k: v for k, v in item.items() if k != 'source'}) for item in config['sources']]
        ops = UpdateOperations(args.directory, sources, settings=Settings(**config.get('settings', {})),
            limits={k: SourceLimits(**v) for k, v in config.get('limits', {}).items()})
    if args.action == 'run':
        if not args.job_id:
            parser.error('run requires a stable --job-id')
        result = ops.run(args.job_id)
    elif args.action == 'tick':
        result = ops.tick()
    else:
        result = ops.status()
    if args.repository is not None:
        from .repository import KnowledgeRepository
        result['publication'] = ops.publication_status(KnowledgeRepository(args.repository, read_only=True))
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(any(v['outcome'] not in {'captured', 'unchanged', 'not_modified'}
                   for v in result.get('sources', {}).values()))


if __name__ == '__main__':
    raise SystemExit(main())

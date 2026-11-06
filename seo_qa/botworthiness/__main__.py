"""Run with python3 -m seo_qa.botworthiness; all inputs and outputs stay local."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .assessment import assess_library, compare, review_template
from .reporting import to_json, write_bundle


def load(path):
    p = Path(path)
    if p.stat().st_size > 10_000_000:
        raise ValueError('Input exceeds 10 MB')
    return json.loads(p.read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser(description='Local AI content quotability; existing SEO scoring unchanged')
    parser.add_argument('command', choices=['assess', 'review-template', 'compare', 'demo'])
    parser.add_argument('--input', help='Local JSON manifest or before-report JSON')
    parser.add_argument('--after', help='After-report JSON for comparison')
    parser.add_argument('--reviews', help='Local JSON review map keyed by stable page ID')
    parser.add_argument('--out', required=True, help='Output directory')
    parser.add_argument('--at', help='Timezone-aware evaluation instant (default: current UTC)')
    args = parser.parse_args()
    at = args.at or datetime.now(timezone.utc).isoformat()
    try:
        if args.command == 'compare':
            if not args.input or not args.after:
                parser.error('compare requires --input and --after')
            result = compare(load(args.input), load(args.after))
            Path(args.out).mkdir(parents=True, exist_ok=True)
            target = Path(args.out)/'comparison.json'
            target.write_text(to_json(result), encoding='utf-8')
            print(target.resolve())
            return
        if args.command == 'demo':
            manifest = {'sampled': True, 'pages': [
                {'id': 'demo-buried', 'evidence_kind': 'synthetic', 'content': '<h1>Sample guide</h1><h2>How often should a filter be checked?</h2><p>It depends on several factors. Check the supplied manual for the specified interval.</p>'},
                {'id': 'demo-answer', 'evidence_kind': 'synthetic', 'content': '<h1>Sample answer</h1><p id="summary">Use the interval specified in the supplied manual.</p><h2>Where is the interval?</h2><p>The supplied manual lists the check interval.</p><h2>What should I record?</h2><ul><li>Check date</li><li>Observed condition</li></ul>'}]}
        else:
            if not args.input:
                parser.error('This command requires --input')
            manifest = load(args.input)
            for p in manifest['pages']:
                if 'content_file' in p:
                    if 'content' in p:
                        raise ValueError('Choose content or content_file, not both')
                    source = Path(args.input).resolve().parent/p.pop('content_file')
                    if source.stat().st_size > 200_000:
                        raise ValueError('Content file exceeds 200 KB')
                    p['content'] = source.read_text(encoding='utf-8')
        if args.command == 'review-template':
            # Validate batch constraints before producing a template.
            assess_library(manifest['pages'], evaluated_at=at,
                           expected_ids=manifest.get('expected_ids'), sampled=manifest.get('sampled', False))
            result = {p['id']: review_template(p, evaluated_at=at) for p in manifest['pages']}
            target = Path(args.out)/'reviews.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(to_json(result), encoding='utf-8')
            print(target.resolve())
            return
        reviews = load(args.reviews) if args.reviews else None
        report = assess_library(manifest['pages'], evaluated_at=at, reviews=reviews,
                                expected_ids=manifest.get('expected_ids'), sampled=manifest.get('sampled', False))
        print(write_bundle(report, args.out))
    except (ValueError, OSError, KeyError, TypeError) as e:
        parser.exit(2, 'Content assessment error: '+str(e)+'\n')


if __name__ == '__main__':
    main()

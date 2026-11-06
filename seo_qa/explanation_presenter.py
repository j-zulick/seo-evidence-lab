"""Pure presentation adapter for score_report criterion items. No scoring or I/O."""
from copy import deepcopy
import json
import html

MODES = ('beginner', 'practitioner', 'engineer')
GLOSSARY = {'criterion': 'A check against an approved requirement.', 'evidence': 'Retained observations supporting a finding.', 'canonical': 'A declared preferred URL; declaration alone does not verify the destination.'}
LABELS = {
 'beginner': ('What was found', 'Why this matters', 'What supports this', 'What to do', 'How to check the change', 'What we cannot conclude'),
 'practitioner': ('Finding', 'Impact', 'Evidence', 'Recommended action', 'Verification', 'Limitations'),
 'engineer': ('Criterion result', 'Requirement impact', 'Evidence identity', 'Remediation', 'Reproduction check', 'Uncertainty and limits')}

def explain(report, mode='beginner'):
    if mode not in MODES:
        raise ValueError('Unsupported explanation mode')
    result = {'mode': mode, 'canonical': deepcopy(report), 'explanations': [], 'glossary': deepcopy(GLOSSARY)}
    scoring = report.get('scoring', report)
    # r4 exposes missed_items, not a complete criterion inventory.
    items = scoring.get('items', scoring.get('missed_items', []))
    result['coverage'] = 'all supplied items' if 'items' in scoring else 'missed criteria only; passing criteria are not supplied by r4 scoring'
    result['uncertainty'] = deepcopy(report.get('knowledge', {}))
    result['completeness_blockers'] = deepcopy(scoring.get('completeness_blockers', []))
    for item in items:
        context = item.get('context') or {}
        evidence = context.get('evidence_sha256')
        limits = ['This explanation does not establish ranking gains or human comprehension.']
        if scoring.get('completeness_blockers'):
            limits.append('Assessment completeness is limited: ' + '; '.join(map(str, scoring['completeness_blockers'])))
        knowledge = report.get('knowledge') or {}
        if knowledge:
            limits.append('Knowledge guidance is advisory; retained applicability uncertainty and freshness are supplied separately without changing findings.')
        if not evidence:
            limits.append('Evidence is missing; gather a retained observation before concluding or changing acceptance.')
        if item.get('conflicting') or report.get('conflicting'):
            limits.append('Evidence conflicts; retain both accounts and resolve applicability before action.')
        if report.get('stale') or report.get('superseded') or item.get('outdated'):
            limits.append('Evidence is historical or outdated; obtain a current capture. Historical results remain unchanged.')
        values = [str(item.get('reason') or 'No finding explanation was retained.'),
                  'This result describes attainment of the approved criterion; business impact requires separate evidence.',
                  json.dumps(context, sort_keys=True, ensure_ascii=False) if evidence else 'No retained evidence identity is available.',
                  str(item.get('next_step') or 'Review the approved criterion and gather missing evidence.'),
                  'Repeat the same criterion against a new retained observation and compare expected and observed values.',
                  ' '.join(limits)]
        result['explanations'].append({'criterion_id': item.get('criterion_id'), 'sections': dict(zip(LABELS[mode], values)), 'glossary_refs': ['criterion', 'evidence'] + (['canonical'] if 'canonical' in str(item.get('criterion_id')) else [])})
    return result

def render(report, mode='beginner', format='json'):
    projection = explain(report, mode)
    if format == 'json':
        return json.dumps(projection, sort_keys=True, ensure_ascii=False, indent=2)
    if format == 'html':
        return '<section>' + ''.join('<article>' + ''.join('<h3>'+html.escape(k)+'</h3><p>'+html.escape(v)+'</p>' for k,v in row['sections'].items()) + '</article>' for row in projection['explanations']) + '<dl>' + ''.join('<dt>'+html.escape(k)+'</dt><dd>'+html.escape(v)+'</dd>' for k,v in GLOSSARY.items()) + '</dl></section>'
    raise ValueError('Unsupported export format')

if __name__ == '__main__':
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser()
    p.add_argument('input'); p.add_argument('--mode', choices=MODES, default='beginner'); p.add_argument('--format', choices=('json','html'), default='json')
    args = p.parse_args()
    print(render(json.loads(Path(args.input).read_text()), args.mode, args.format))

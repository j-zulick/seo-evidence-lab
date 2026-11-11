"""Reproduce a synthetic saved-news import and unapproved guidance-impact proposal."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from seo_qa.news_bridge import import_saved, report, propose


def run(out):
    out.mkdir(parents=True, exist_ok=True)
    db = out/'inbox.sqlite'
    imports = [import_saved(db, (ROOT/'fixtures/news-bridge'/name).read_bytes(), synthetic=True)
               for name in ['saved-email.json', 'saved-email-revised.json']]
    inbox = report(db)
    version = next(x['version_id'] for x in inbox['items'] if x['url'] == 'https://publisher.example/guide')
    inputs = json.loads((ROOT/'fixtures/news-bridge/audit-inputs.json').read_text())
    proposal = propose(db, version, inputs, 'question_heading', 'Synthetic software demonstration',
        'Synthetic example mapping an excerpt to a candidate heading rule; no human corroboration or activation.')
    (out/'inbox-report.json').write_text(json.dumps(inbox, indent=2)+'\n')
    (out/'guidance-proposal.json').write_text(json.dumps(proposal, indent=2)+'\n')
    result = {'imports': imports, 'proposal_id': proposal['id'], 'active': proposal['active'],
              'human_approval': proposal['human_approval'], 'network_calls': 0}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('demo-output/news'))
    run(parser.parse_args().output)

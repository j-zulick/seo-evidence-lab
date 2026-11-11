"""Frozen authored fixture comparison; no independent human accuracy claim."""
import json
from .competitor import digest
from .evidence_search import load, index, search
from .release_retrieval import expanded_search, VERSION

HELD_OUT = [
    {'query': 'reactions', 'expected': ['speed']},
    {'query': 'user reactions', 'expected': ['speed']},
    {'query': 'page responsiveness', 'expected': ['speed']},
    {'query': 'URL address', 'expected': ['canonical']},
    {'query': 'duplicate address', 'expected': ['canonical']},
    {'query': 'exclusion directive', 'expected': ['robots']},
    {'query': 'preferred duplicate', 'expected': ['canonical']},
    {'query': 'noindex requests exclusion', 'expected': ['robots']},
    {'query': 'permits prohibits archive', 'expected': ['conflict-a', 'conflict-b']},
    {'query': 'INP interaction', 'expected': ['speed']},
    {'query': 'astrophysical kumquats', 'expected': []},
    {'query': '!!!', 'expected': []},
]


def run():
    corpus = load()
    db = index(corpus)
    groups = {}
    try:
        for name, cases in [('original_six', corpus['queries']), ('held_out_authored', HELD_OUT)]:
            rows = []
            for case in cases:
                row = dict(case)
                for mode, engine in [('baseline', search), ('expanded', expanded_search)]:
                    ids = [x['id'] for x in engine(db, case['query'])]
                    row[mode] = {'ids': ids, 'passed': all(i in ids for i in case['expected']) if case['expected'] else not ids}
                rows.append(row)
            groups[name] = {'cases': rows, 'total': len(rows), **{
                mode + '_passed': sum(row[mode]['passed'] for row in rows) for mode in ('baseline', 'expanded')}}
    finally:
        db.close()
    return {'retrieval_version': VERSION, 'corpus_sha256': digest(corpus), 'held_out_sha256': digest(HELD_OUT),
            'groups': groups, 'limitations': 'Small synthetic corpus and agent-authored labels. Held-out queries authored before execution; shared vocabulary, not independent field evaluation. Fixed lexical expansion only; ranking or citation outcomes unmeasured.'}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))

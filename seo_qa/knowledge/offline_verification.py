"""Manual synthetic local audit smoke command; never hosted acceptance."""
import argparse
import json
from datetime import date
from pathlib import Path

from ..fixtures import seed, fixture_inspector
from ..workflow import build_workflow
from .audit_pin import KnowledgeStore
from .local_demo import run_demo
from .repository import KnowledgeRepository
from .retrieval import Context


def run(state_dir):
    demo = run_demo(state_dir)
    repository = KnowledgeRepository(demo['repository'], read_only=True)
    context = Context(date(2026, 11, 4), provider='fixture-provider',
                      search_engine='fixture-engine', jurisdiction='US', platform_version='v2',
                      true_conditions=frozenset({'html-response'}))
    store = KnowledgeStore(Path(demo['repository']).parent / 'audits.sqlite',
                           repository=repository, context=context)
    requirement = seed(store)[0]
    output = build_workflow(store, fixture_inspector).invoke({'requirement_id': requirement['id']})
    report = output['report']
    if not report['knowledge']['explanations']:
        raise RuntimeError('Expected synthetic cited guidance')
    return dict(mode='synthetic-local', knowledge_demo=demo,
                run_id=output['run_id'], knowledge=report['knowledge'],
                findings=report['findings'], live_integrations_executed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    print(json.dumps(run(parser.parse_args().state_dir), indent=2))

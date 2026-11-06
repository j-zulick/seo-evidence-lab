import argparse
import json
from pathlib import Path
from .store import Store
from .fixtures import seed,fixture_inspector


def main():
    parser=argparse.ArgumentParser(description='SEO QA Tool — local dummy MVP')
    parser.add_argument('command',choices=['serve','qa','demo','recover','connections','knowledge-demo'])
    parser.add_argument('--state',default='state/local.sqlite')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--knowledge-dir', default='state/knowledge')
    parser.add_argument('--knowledge-repository', help='Verified local knowledge database for advisory audit pins')
    parser.add_argument('--knowledge-context', help='JSON applicability context with explicit evaluated_on date')
    args=parser.parse_args()
    if args.command == 'knowledge-demo':
        from .knowledge.local_demo import run_demo
        print(json.dumps(run_demo(args.knowledge_dir), indent=2))
        return
    if args.command=='connections':
        from .connection_config import load_config
        print(json.dumps(load_config().status(),indent=2))
        return
    if bool(args.knowledge_repository) != bool(args.knowledge_context):
        parser.error('Knowledge repository and context must be supplied together')
    if args.knowledge_repository:
        from datetime import date
        from .knowledge.audit_pin import KnowledgeStore
        from .knowledge.repository import KnowledgeRepository
        from .knowledge.retrieval import Context
        fields = json.loads(Path(args.knowledge_context).read_text())
        fields['evaluated_on'] = date.fromisoformat(fields['evaluated_on'])
        fields['true_conditions'] = frozenset(fields.get('true_conditions', ()))
        store = KnowledgeStore(args.state,
                               repository=KnowledgeRepository(args.knowledge_repository, read_only=True),
                               context=Context(**fields))
    else:
        from .knowledge.config import configured_store
        store=configured_store(args.state)
    if args.command in {'serve', 'qa'}:
        import uvicorn
        from .web import create_app
        if args.command == 'serve':
            seed(store)
        app=create_app(store,inspector_module=fixture_inspector)
        connection=app.state.connection
        try:
            if args.command == 'serve' and connection.config.enabled and connection.auth.status().get('owner_id'):
                connection.connect()
                app.state.store=connection.backend
        except (ValueError,OSError):
            print('Saved Supabase connection needs attention; open /connections to reconnect.')
        if args.command == 'qa':
            print(f'SEO QA workspace: http://127.0.0.1:{args.port}/qa')
        uvicorn.run(app,host='127.0.0.1',port=args.port)
    elif args.command=='demo':
        from .workflow import build_workflow
        reqs=seed(store)
        graph=build_workflow(store,fixture_inspector)
        records=[]
        for req in reqs:
            state=graph.invoke({'requirement_id':req['id']})
            report=state['report']
            decision='accepted' if all(f['outcome'] in ('pass','not_applicable') for f in report['findings']) else 'rejected'
            store.review(state['run_id'],decision,reason='Fixture demonstration; not live SEO acceptance')
            records.append(store.export_report(state['run_id']))
        path=Path('artifacts/demo.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(records,indent=2))
        print(json.dumps({'reports':str(path.resolve()),'runs':len(records),'mode':'dummy','accepted':sum(x['acceptance_current'] for x in records)}))
    else:
        print(json.dumps({'reconciled_interrupted_jobs':store.recover_expired()}))


if __name__=='__main__':main()

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from .botworthiness.local_web import local_available
from .competitor import demo, ROOT
from .measurement import fixture_view
from .evidence_search import index, load, search, benchmark
from .timeline_eval import evaluate


def install_routes(app, env):
    def snapshot():
        if not local_available(app):
            raise HTTPException(404)
        return demo() | {"measurement": fixture_view()}

    @app.get('/qa/lab', response_class=HTMLResponse)
    def lab():
        data = snapshot()
        return env.get_template('lab.html').render(data=data, active='overview')

    @app.get('/qa/walkthrough', response_class=HTMLResponse)
    def article_walkthrough():
        if not local_available(app):
            raise HTTPException(404)
        article = (ROOT/'fixtures/walkthrough/article.html').read_text()
        return env.get_template('article_walkthrough.html').render(article=article, active='overview')

    @app.get('/qa/walkthrough/{kind}.json')
    def article_inputs(kind: str):
        if not local_available(app) or kind not in ('library', 'reviews'):
            raise HTTPException(404)
        import json
        return json.loads((ROOT/'fixtures/walkthrough'/(kind+'.json')).read_text())

    @app.get('/qa/competitors', response_class=HTMLResponse)
    def timeline():
        return env.get_template('competitor.html').render(data=snapshot(), active='timeline')

    def evaluation_data():
        if not local_available(app):
            raise HTTPException(404)
        experiment = evaluate()
        retrieval = benchmark()
        rows = experiment['report']['cases']
        return {
            'experiment': experiment['report'],
            'inputs': {item['id']: item['input'] for item in experiment['dataset']['cases']},
            'baseline_passed': sum(row['baseline_pass'] for row in rows),
            'revised_passed': sum(row['revised_pass'] for row in rows),
            'retrieval': retrieval,
            'retrieval_passed': sum(row['all_expected_in_top_three'] for row in retrieval['queries']),
        }

    @app.get('/qa/evaluations', response_class=HTMLResponse)
    def evaluations():
        return env.get_template('evaluation.html').render(data=evaluation_data(), active='evaluation')

    @app.get('/qa/evaluations/report.json')
    def evaluations_report():
        return JSONResponse(evaluation_data(), headers={
            'Cache-Control': 'no-store',
            'Content-Disposition': 'attachment; filename="evaluation-results.json"',
        })

    @app.get('/qa/community-news', response_class=HTMLResponse)
    def community_news():
        if not local_available(app):
            raise HTTPException(404)
        from .community_news import fixture
        return env.get_template('community_news.html').render(data=fixture(), active='news')

    @app.get('/qa/evidence-search', response_class=HTMLResponse)
    def evidence_search(q: str = ''):
        if not local_available(app):
            raise HTTPException(404)
        if len(q) > 512:
            raise HTTPException(400, 'Query exceeds 512 characters')
        db = index(load())
        try:
            hits = search(db, q)
        finally:
            db.close()
        return env.get_template('evidence_search.html').render(query=q, hits=hits, active='search')

    @app.get('/qa/competitors/comparison.json')
    def comparison():
        return snapshot()

    @app.get('/qa/competitors/evidence/{audit_id}')
    def evidence(audit_id: str):
        result = snapshot()['comparison']
        for key in ('before','after'):
            if result[key]['id']==audit_id:
                return result[key]
        raise HTTPException(404)

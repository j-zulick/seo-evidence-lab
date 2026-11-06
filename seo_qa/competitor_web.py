from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from .botworthiness.local_web import local_available
from .competitor import demo
from .measurement import fixture_view
from .evidence_search import index, load, search


def install_routes(app, env):
    def snapshot():
        if not local_available(app):
            raise HTTPException(404)
        return demo() | {"measurement": fixture_view()}

    @app.get('/qa/competitors', response_class=HTMLResponse)
    def timeline():
        return env.get_template('competitor.html').render(data=snapshot())

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
        return env.get_template('evidence_search.html').render(query=q, hits=hits)

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

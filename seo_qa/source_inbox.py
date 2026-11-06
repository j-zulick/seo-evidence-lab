"""Manual local triage of a saved change. Decisions never publish guidance."""
from pathlib import Path
import sqlite3
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse
from .competitor import demo,digest
from .botworthiness.local_web import local_available
from .request_context import request_csrf


def install(app,env,csrf_check):
    def item():
        if not local_available(app):
            raise HTTPException(404)
        data=demo()
        return {'key':digest(data['diff']), 'diff':data['diff'], 'url':data['series'][0]['request']['target']}

    def database():
        return Path(str(app.state.store.path)+'.source-inbox.sqlite')

    @app.get('/qa/source-inbox')
    def inbox():
        entry=item()
        decisions=[]
        if database().exists():
            with sqlite3.connect(database()) as db:
                decisions=db.execute('SELECT action, recorded FROM decisions WHERE item=? ORDER BY rowid DESC LIMIT 20',(entry['key'],)).fetchall()
        from fastapi.responses import HTMLResponse
        return HTMLResponse(env.get_template('source_inbox.html').render(entry=entry,decisions=decisions,csrf=request_csrf(app.state.csrf_token)))

    @app.post('/qa/source-inbox')
    def decide(key:str=Form(...), action:str=Form(...), csrf_token:str=Form(...)):
        entry=item()
        csrf_check(csrf_token)
        if action not in ('accept','defer') or key!=entry['key']:
            raise HTTPException(400,'Invalid inbox action or stale change')
        with sqlite3.connect(database()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS decisions(item TEXT,action TEXT,recorded TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO decisions(item,action) VALUES(?,?)',(key,action))
        return RedirectResponse('/qa/source-inbox',status_code=303)

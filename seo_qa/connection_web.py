"""Loopback-only, CSRF-protected personal sign-in forms. Never returns tokens."""
from __future__ import annotations
from fastapi import Form, HTTPException
from fastapi.responses import RedirectResponse
from .supabase_auth import AuthError
from .supabase_adapter import SupabaseError


def install_connection_routes(app, render, csrf_check, runtime):
    app.state.connection = runtime

    @app.get('/connections')
    def connections():
        return render('connections', connection=runtime.status(), message=None)

    def dispatch(action, email='', password=''):
        try:
            if action == 'sign_up':
                result = runtime.sign_up(email, password)
            elif action == 'sign_in':
                result = runtime.sign_in(email, password)
            elif action == 'connect':
                result = runtime.connect()
            elif action == 'sign_out':
                result = runtime.sign_out()
            else:
                raise HTTPException(404)
        except (AuthError, SupabaseError, ValueError, OSError):
            # Provider bodies and credentials must not reach the UI/error logger.
            app.state.store = runtime.backend
            response = render('connections', connection=runtime.status(),
                              message='The connection could not be verified. Check your sign-in, confirm your email if required, then try connecting again.')
            response.status_code = 400
            return response
        app.state.store = runtime.backend
        if result.get('state') == 'confirmation_required':
            return render('connections', connection=runtime.status(),
                          message='Complete the email confirmation if requested, then sign in here. Your password was not saved.')
        return RedirectResponse('/connections', status_code=303)

    @app.post('/connections/sign-up')
    def sign_up(email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        return dispatch('sign_up', email, password)

    @app.post('/connections/sign-in')
    def sign_in(email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        return dispatch('sign_in', email, password)

    @app.post('/connections/connect')
    def connect(csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        return dispatch('connect')

    @app.post('/connections/sign-out')
    def sign_out(csrf_token: str = Form(...)):
        csrf_check(csrf_token)
        return dispatch('sign_out')

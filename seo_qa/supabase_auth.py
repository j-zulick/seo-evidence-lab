"""Explicit owner-driven Supabase Auth. No admin keys, password storage or logging.

REST contracts checked against supabase/auth/openapi.yaml on 2026-11-02.
No real Auth call occurs at import/construction. Session files must remain in
ignored app/state, private to the OS owner. Refresh operations are serialized.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from .connection_config import ConnectionConfig

ROOT=Path(__file__).resolve().parents[1]


class AuthError(ValueError):
    """Messages intentionally omit user input, tokens and provider response bodies."""


class RefreshUncertain(AuthError):
    """A refresh may have rotated a token; require explicit sign-in, not retry."""


class SupabaseAuth:
    def __init__(self,config:ConnectionConfig,*,session_file=None,enabled=False,client=None):
        self._config=config
        self._enabled=enabled is True
        self._client=client
        self._owns_client=client is None
        self._session_file=Path(session_file or ROOT/'state/auth/supabase-user-session.json')
        if not self._session_file.resolve().is_relative_to((ROOT/'state').resolve()):
            raise AuthError('Session file must stay in ignored application state directory')
        if self._session_file.is_symlink():raise AuthError('Session file cannot be a symlink')
        if self._enabled:
            ref=config.project_ref
            u=urlsplit(config.url)
            if (not re.fullmatch(r'[a-z0-9]{20}',ref) or u.scheme!='https'
                or u.netloc!=ref+'.supabase.co' or u.path not in ('','/') or u.query or u.fragment):
                raise AuthError('Explicit matching HTTPS Supabase project required')
            if not config.publishable_key.startswith('sb_publishable_'):
                raise AuthError('Only a publishable project key is permitted')

    def __repr__(self):return f'SupabaseAuth(enabled={self._enabled}, secrets=redacted)'

    def close(self):
        if self._owns_client and self._client is not None:self._client.close()

    def _require(self):
        if not self._enabled:raise AuthError('Owner Auth flow is disabled')

    @contextmanager
    def _locked(self):
        self._require()
        self._session_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        lock=self._session_file.with_suffix('.lock')
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        try:fd=os.open(lock,flags,0o600)
        except OSError:raise AuthError('Private session lock unavailable') from None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
                raise AuthError('Private session lock permissions invalid')
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    def _request(self,method,path,*,token=None,body=None,params=None):
        self._require()
        if self._client is None:self._client=httpx.Client(timeout=15,follow_redirects=False,trust_env=False)
        headers={'apikey':self._config.publishable_key}
        if token:headers['Authorization']='Bearer '+token
        try:
            response=self._client.request(method,self._config.url.rstrip('/')+'/auth/v1'+path,
                headers=headers,json=body,params=params,timeout=15,follow_redirects=False)
        except httpx.RequestError:
            raise AuthError('Auth request failed; no sensitive response details retained') from None
        if not 200<=response.status_code<300:
            raise AuthError('Auth operation refused or unavailable (HTTP '+str(response.status_code)+')') from None
        if response.status_code==204:return {}
        if len(response.content)>64*1024:raise AuthError('Auth response exceeds bounded size')
        try:data=response.json()
        except ValueError:raise AuthError('Auth response is not valid JSON') from None
        if not isinstance(data,dict):raise AuthError('Auth response has unexpected shape')
        return data

    def _credentials(self,email,password):
        if (not isinstance(email,str) or len(email)>320 or '@' not in email
                or any(ord(c)<32 for c in email)):
            raise AuthError('A valid bounded email address is required')
        if not isinstance(password,str) or not 1<=len(password)<=4096:
            raise AuthError('A nonempty bounded password is required')
        return {'email':email.strip(),'password':password}

    def _claims(self,token,*,allow_expired=False):
        try:
            if not isinstance(token,str) or len(token)>16384:raise ValueError()
            parts=token.split('.')
            if len(parts)!=3 or not all(parts):raise ValueError()
            claims=json.loads(base64.urlsafe_b64decode(parts[1]+'='*(-len(parts[1])%4)))
            owner=str(UUID(claims['sub']))
            if type(claims['exp']) not in (int,float):raise ValueError()
            expires=float(claims['exp'])
            if (claims.get('role')!='authenticated' or claims.get('iss')!=self._config.url.rstrip('/')+'/auth/v1'
                or not math.isfinite(expires) or expires<=0
                or (not allow_expired and expires<=time.time())):raise ValueError()
            if self._config.owner_id and owner!=self._config.owner_id:raise ValueError()
            return owner,expires
        except (ValueError,TypeError,KeyError,UnicodeError,OverflowError):
            raise AuthError('Session does not match the selected project/owner or has expired') from None

    def _verify(self,token,owner):
        user=self._request('GET','/user',token=token)
        if user.get('id')!=owner:raise AuthError('Auth did not verify the expected owner')

    def _save(self,session):
        if self._session_file.is_symlink():raise AuthError('Session file cannot be a symlink')
        fd,name=tempfile.mkstemp(prefix='.session-',dir=self._session_file.parent)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as out:
                json.dump(session,out,separators=(',',':'));out.flush();os.fsync(out.fileno())
            os.replace(name,self._session_file)
        finally:
            if os.path.exists(name):os.unlink(name)

    def _read(self):
        try:fd=os.open(self._session_file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        except FileNotFoundError:return None
        except OSError:raise AuthError('Private session unavailable') from None
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>64*1024):
                raise AuthError('Private session permissions or size invalid')
            with os.fdopen(fd,'r',encoding='utf-8') as stream:
                fd=None
                try:session=json.load(stream)
                except (ValueError,UnicodeError):raise AuthError('Private session is invalid') from None
        finally:
            if fd is not None:os.close(fd)
        if (not isinstance(session,dict) or session.get('project_ref')!=self._config.project_ref
                or session.get('url')!=self._config.url.rstrip('/')):
            raise AuthError('Saved session belongs to a different project')
        fields={'project_ref','url','owner_id','access_token','refresh_token','expires_at','owner_verified_at','state'}
        if set(session)!=fields or session['state'] not in ('saved','refresh_uncertain'):
            raise AuthError('Saved session has an invalid shape or state')
        for name in ('expires_at','owner_verified_at'):
            value=session[name]
            try:
                valid=type(value) in (int,float) and math.isfinite(value) and value>0
            except (OverflowError,TypeError):valid=False
            if not valid:raise AuthError('Saved session contains invalid time metadata')
        owner,expires=self._claims(session['access_token'],allow_expired=True)
        if owner!=session['owner_id'] or expires!=session['expires_at']:
            raise AuthError('Saved session identity or expiry does not match its token')
        if not isinstance(session['refresh_token'],str) or not 1<=len(session['refresh_token'])<=8192:
            raise AuthError('Saved session contains an invalid refresh token')
        return session

    def _accept(self,data,expected_owner=None):
        token=data.get('access_token');refresh=data.get('refresh_token')
        owner,expires=self._claims(token)
        if expected_owner and owner!=expected_owner:raise AuthError('Refreshed session changed owner')
        if not isinstance(refresh,str) or not 1<=len(refresh)<=8192:
            raise AuthError('Auth did not return a bounded refresh token')
        self._verify(token,owner)
        session={'project_ref':self._config.project_ref,'url':self._config.url.rstrip('/'),
            'owner_id':owner,'access_token':token,'refresh_token':refresh,'expires_at':expires,
            'owner_verified_at':time.time(),'state':'saved'}
        self._save(session)
        return self._summary(session)

    def _summary(self,session):
        return {'state':'authenticated','authenticated':True,'project_ref':self._config.project_ref,
                'owner_id':session['owner_id'],'expires_at':session['expires_at'],
                'hosted_store_backend':False}

    def status(self):
        if not self._enabled:return {'state':'disabled','authenticated':False,'owner_id':None,'expires_at':None,'hosted_store_backend':False}
        session=self._read()
        if session is None:return {'state':'signed_out','authenticated':False,'owner_id':None,'expires_at':None,'hosted_store_backend':False}
        expired=session.get('expires_at',0)<=time.time()
        valid=False
        if not expired and session.get('state')=='saved':
            try:valid=self._claims(session.get('access_token'))[0]==session.get('owner_id')
            except AuthError:pass
        return {'state':session.get('state','saved'),'authenticated':valid,'expired':expired,
                'owner_id':session['owner_id'],'expires_at':session['expires_at'],'owner_verified_at':session['owner_verified_at'],
                'hosted_store_backend':False,'note':'Saved session metadata; not a current network check'}

    def sign_up(self,email,password):
        with self._locked():
            data=self._request('POST','/signup',body=self._credentials(email,password))
            if data.get('access_token'):return self._accept(data)
            # Signup may return an obfuscated user for an existing address.
            # Never claim an account was created or identity verified from it.
            return {'state':'confirmation_required','authenticated':False,'message':'If applicable, complete email confirmation, then sign in.','hosted_store_backend':False}

    def sign_in(self,email,password):
        with self._locked():
            data=self._request('POST','/token',params={'grant_type':'password'},body=self._credentials(email,password))
            return self._accept(data)

    def _refresh(self,session):
        if session.get('state')=='refresh_uncertain':
            raise RefreshUncertain('Previous refresh outcome is uncertain; explicitly sign in again')
        refresh=session.get('refresh_token')
        if not isinstance(refresh,str) or not refresh:raise AuthError('No refresh session available')
        try:
            data=self._request('POST','/token',params={'grant_type':'refresh_token'},body={'refresh_token':refresh})
            return self._accept(data,expected_owner=session.get('owner_id'))
        except AuthError:
            # Rotation may have happened even if transport/readback failed.
            self._save(dict(session,state='refresh_uncertain'))
            raise RefreshUncertain('Refresh could not be verified; explicitly sign in again') from None

    def refresh(self):
        with self._locked():
            session=self._read()
            if session is None:raise AuthError('Sign in before refreshing a session')
            return self._refresh(session)

    def connection_config(self,*,refresh_if_needed=True):
        with self._locked():
            session=self._read()
            if session is None:raise AuthError('Owner sign-in is required')
            if session.get('state')=='refresh_uncertain':
                raise RefreshUncertain('Previous refresh outcome is uncertain; explicitly sign in again')
            if session.get('expires_at',0)<=time.time()+30:
                if not refresh_if_needed:raise AuthError('Session is expired or expiring; refresh required')
                self._refresh(session);session=self._read()
            owner,_=self._claims(session.get('access_token'))
            self._verify(session['access_token'],owner)
            return replace(self._config,user_jwt=session['access_token'],owner_id=owner,enabled=True)

    def sign_out(self):
        with self._locked():
            remote='not_attempted'
            try:
                session=self._read()
                if session:
                    self._request('POST','/logout',params={'scope':'local'},token=session.get('access_token'))
                    remote='confirmed'
            except AuthError:remote='unverified'
            finally:
                try:self._session_file.unlink()
                except FileNotFoundError:pass
            return {'state':'signed_out','authenticated':False,'remote_revocation':remote,'hosted_store_backend':False}

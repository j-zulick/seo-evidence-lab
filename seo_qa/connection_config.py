"""Secret-safe optional connection config; never switches the local Store.

This module does not log in, validate a live session, change MCP config or make
network requests. The transport factory supports root rows/private evidence; HostedStore adds the normalized workflow client.
"""
from __future__ import annotations

from dataclasses import dataclass,field
import os
from pathlib import Path
import re
import stat
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID


class ConnectionConfigError(ValueError):
    """Intentionally contains field names/reasons, never rejected values."""


FIELDS=frozenset({'SUPABASE_URL','SUPABASE_PUBLISHABLE_KEY','SUPABASE_USER_JWT',
    'SEO_SUPABASE_PROJECT_REF','SEO_SUPABASE_OWNER_ID','SEO_SUPABASE_ADAPTER_ENABLED',
    'SEO_DATA_MODE','SEO_EVIDENCE_BUCKET'})
ROOT=Path(__file__).resolve().parents[1]


def _private_values(path:Path)->dict[str,str]:
    """Read only private, owner-controlled dotenv with a deliberately small grammar."""
    try:
        info=path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ConnectionConfigError('Private environment must be a regular non-symlink file')
    if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ConnectionConfigError('Private environment requires owner-only file permissions')
    if info.st_size>64*1024:
        raise ConnectionConfigError('Private environment file exceeds size limit')
    values={}
    for line in path.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ConnectionConfigError('Private environment has an invalid assignment')
        key,value=line.split('=',1);key=key.strip();value=value.strip()
        if key not in FIELDS:
            # Other app settings may share .env, but are not loaded here.
            continue
        if key in values:
            raise ConnectionConfigError('Private environment contains a duplicate connection field')
        if value[:1] in ('"',"'"):
            if len(value)<2 or value[-1]!=value[0]:
                raise ConnectionConfigError('Private environment has invalid quoting')
            value=value[1:-1]
        if any(ord(c)<32 for c in value):
            raise ConnectionConfigError('Private environment contains control characters')
        values[key]=value
    return values


def write_private_env(path:Path,values:Mapping[str,str])->None:
    """Explicit, create-only helper. No overwriting or rotating existing secrets."""
    if set(values)-FIELDS:
        raise ConnectionConfigError('Unsupported connection field')
    if any(not isinstance(v,str) or '\n' in v or '\r' in v or '\x00' in v for v in values.values()):
        raise ConnectionConfigError('Connection values must be single-line strings')
    # Exclusive creation plus NOFOLLOW avoids overwriting an existing file/link.
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(Path(path),flags,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as out:
        for key,value in values.items():
            # Values are literal; this module never performs shell expansion.
            out.write(key+'='+value+'\n')


@dataclass(frozen=True)
class ConnectionConfig:
    url:str=''
    project_ref:str=''
    publishable_key:str=field(default='',repr=False)
    user_jwt:str=field(default='',repr=False)
    owner_id:str=''
    enabled:bool=False
    bucket:str='seo-evidence-private'
    data_mode:str='dummy'

    def status(self)->dict:
        configured={'project':bool(self.url and self.project_ref),
                    'publishable_key':bool(self.publishable_key),
                    'owner_session':bool(self.user_jwt and self.owner_id)}
        return {'application_mode':'local-dummy','hosted_adapter_enabled':self.enabled,
                'connection_fields_present':configured,
                'configuration_complete':all(configured.values()),
                'hosted_verified':False,
                'implemented_hosted_scope':['root_project_row','private_evidence_objects','normalized_workflow_rpc_client'],
                'hosted_store_backend':False,
                'limits':['Configuration presence is not live authentication or RLS verification.',
                          'The app selects the hosted Store only after owner sign-in and live verification; configuration alone does not switch persistence.']}

    def build_adapter(self,*,client=None):
        """Construct only. The adapter validates session through Auth when used."""
        if not self.enabled:
            raise ConnectionConfigError('Hosted adapter is disabled')
        if not self.status()['configuration_complete']:
            raise ConnectionConfigError('Hosted connection fields are incomplete')
        from .supabase_adapter import SupabaseAdapter
        return SupabaseAdapter(url=self.url,project_ref=self.project_ref,
            publishable_key=self.publishable_key,user_jwt=self.user_jwt,
            owner_id=self.owner_id,enabled=True,client=client)


def load_config(*,environ:Mapping[str,str]|None=None,env_file:Path|None=None)->ConnectionConfig:
    values=_private_values(Path(env_file) if env_file is not None else ROOT/'.env')
    source=os.environ if environ is None else environ
    values.update({k:v for k,v in source.items() if k in FIELDS})
    if any(not isinstance(v,str) for v in values.values()):
        raise ConnectionConfigError('Connection configuration must contain strings')
    enabled=values.get('SEO_SUPABASE_ADAPTER_ENABLED','false').strip().lower()
    if enabled not in ('true','false'):
        raise ConnectionConfigError('SEO_SUPABASE_ADAPTER_ENABLED must be true or false')
    mode=values.get('SEO_DATA_MODE','dummy')
    if mode!='dummy':
        raise ConnectionConfigError('Only dummy application mode is implemented')
    bucket=values.get('SEO_EVIDENCE_BUCKET','seo-evidence-private')
    if bucket!='seo-evidence-private':
        raise ConnectionConfigError('Only the implemented private evidence bucket is supported')
    ref=values.get('SEO_SUPABASE_PROJECT_REF','').strip()
    url=values.get('SUPABASE_URL','').strip().rstrip('/')
    if ref and not re.fullmatch(r'[a-z0-9]{20}',ref):
        raise ConnectionConfigError('Invalid SEO_SUPABASE_PROJECT_REF')
    if url:
        parsed=urlsplit(url)
        if (not ref or parsed.scheme!='https' or parsed.netloc!=ref+'.supabase.co'
            or parsed.path or parsed.query or parsed.fragment):
            raise ConnectionConfigError('SUPABASE_URL must match the explicitly selected project')
    owner=values.get('SEO_SUPABASE_OWNER_ID','').strip()
    if owner:
        try:owner=str(UUID(owner))
        except ValueError as exc:raise ConnectionConfigError('Invalid SEO_SUPABASE_OWNER_ID') from exc
    key=values.get('SUPABASE_PUBLISHABLE_KEY','')
    if key and not key.startswith('sb_publishable_'):
        raise ConnectionConfigError('Only publishable API keys are supported')
    return ConnectionConfig(url=url,project_ref=ref,publishable_key=key,
        user_jwt=values.get('SUPABASE_USER_JWT',''),owner_id=owner,
        enabled=enabled=='true',bucket=bucket,data_mode=mode)

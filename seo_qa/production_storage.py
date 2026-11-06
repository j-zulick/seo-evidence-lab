"""Explicit environment-only, read-only-at-startup hosted Store factory.

The existing database remains a dummy workflow profile. This module neither
creates that profile nor reads local dotenv, browser or saved session files.
"""
from __future__ import annotations

import os
from uuid import NAMESPACE_URL, UUID, uuid5

from .hosted_store import HostedStore
from .supabase_adapter import SupabaseAdapter

PROJECT_REF = 'mqzslbwdszcbsyppdlfz'


def create_store(*, environ=None, client=None):
    values = os.environ if environ is None else environ
    required = ('SEO_SUPABASE_PROJECT_REF', 'SUPABASE_URL',
                'SUPABASE_PUBLISHABLE_KEY', 'SUPABASE_USER_JWT',
                'SEO_SUPABASE_OWNER_ID')
    if any(not isinstance(values.get(key), str) or not values[key].strip()
           for key in required):
        raise ValueError('Hosted production environment is incomplete')
    if (values['SEO_SUPABASE_PROJECT_REF'] != PROJECT_REF
            or values['SUPABASE_URL'].rstrip('/') != f'https://{PROJECT_REF}.supabase.co'):
        raise ValueError('Only the existing dedicated SEO QA project is supported')
    if values.get('SEO_DATA_MODE', 'dummy') != 'dummy':
        raise ValueError('Only the existing hosted dummy workflow profile is supported')
    try:
        owner = str(UUID(values['SEO_SUPABASE_OWNER_ID']))
    except ValueError:
        raise ValueError('Invalid owner identity') from None
    api = SupabaseAdapter(url=values['SUPABASE_URL'], project_ref=PROJECT_REF,
                          publishable_key=values['SUPABASE_PUBLISHABLE_KEY'],
                          user_jwt=values['SUPABASE_USER_JWT'], owner_id=owner,
                          enabled=True, client=client)
    root = str(uuid5(NAMESPACE_URL, 'seo-qa-tool:' + PROJECT_REF + ':' + owner))
    try:
        # HostedStore verifies auth and the existing root. Never create/seed it.
        from .scoped_workflow import flag
        from .knowledge.config import compose_hosted
        return compose_hosted(HostedStore(api, root), scoped_enabled=flag(values), environ=values)
    except BaseException:
        api.close()
        raise

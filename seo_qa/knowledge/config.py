"""Server-only explicit knowledge configuration; absent values never imply freshness."""
from datetime import date
import json
import os

from .remote import KnowledgeUnavailable, RemoteConfig, SupabaseKnowledgeRepository
from .retrieval import Context


def configuration(values):
    try:
        config = RemoteConfig(
            project_ref=values['SEO_KNOWLEDGE_PROJECT_REF'],
            table=values['SEO_KNOWLEDGE_TABLE'],
            release_id=values['SEO_KNOWLEDGE_RELEASE_ID'],
            publishable_key=values['SEO_KNOWLEDGE_PUBLISHABLE_KEY'],
            user_jwt=values['SEO_KNOWLEDGE_USER_JWT'])
        fields = json.loads(values['SEO_KNOWLEDGE_CONTEXT'])
        fields['evaluated_on'] = date.fromisoformat(fields['evaluated_on'])
        fields['true_conditions'] = frozenset(fields.get('true_conditions', ()))
        context = Context(**fields)
    except (KeyError, ValueError, TypeError):
        raise KnowledgeUnavailable('Knowledge is enabled but unconfigured') from None
    return SupabaseKnowledgeRepository(config), context


def configured_store(path, *, environ=None, **kwargs):
    values = os.environ if environ is None else environ
    enabled = values.get('SEO_KNOWLEDGE_ENABLED', 'false')
    if enabled == 'false':
        from ..store import Store
        return Store(path, **kwargs)
    if enabled != 'true':
        raise KnowledgeUnavailable('Invalid knowledge enablement flag')
    repository, context = configuration(values)
    from .audit_pin import KnowledgeStore
    from .freshness import assess_release, decode_inputs
    try:
        inputs = decode_inputs(json.loads(values.get('SEO_KNOWLEDGE_FRESHNESS', '{}')))
    except (TypeError, ValueError):
        raise KnowledgeUnavailable('Invalid knowledge freshness configuration') from None
    return KnowledgeStore(path, repository=repository, context=context,
                          freshness=assess_release, freshness_inputs=inputs, **kwargs)


def compose_hosted(legacy, *, scoped_enabled, environ=None):
    values = os.environ if environ is None else environ
    if values.get('SEO_KNOWLEDGE_ENABLED', 'false') == 'false':
        from ..scoped_workflow import compose
        return compose(legacy, enabled=scoped_enabled)
    if values.get('SEO_KNOWLEDGE_ENABLED') != 'true' or not scoped_enabled:
        raise KnowledgeUnavailable('Hosted knowledge requires explicit scoped workflow enablement')
    repository, context = configuration(values)
    from .hosted import HostedKnowledgeStore, KnowledgeCompositeStore
    from .freshness import assess_release, decode_inputs
    try:
        inputs = decode_inputs(json.loads(values.get('SEO_KNOWLEDGE_FRESHNESS', '{}')))
    except (TypeError, ValueError):
        raise KnowledgeUnavailable('Invalid knowledge freshness configuration') from None
    admissions = values.get('SEO_KNOWLEDGE_ADMISSIONS_ENABLED', 'true')
    if admissions not in {'true', 'false'}:
        raise KnowledgeUnavailable('Invalid knowledge admission flag')
    scoped = HostedKnowledgeStore(legacy.api, legacy.project_id, repository=repository,
                                  context=context, freshness=assess_release,
                                  freshness_inputs=inputs, admissions_enabled=admissions == 'true')
    return KnowledgeCompositeStore(legacy, scoped)

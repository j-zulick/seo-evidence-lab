"""Opt-in lexical expansion; never a semantic truth or citation measurement."""
from .evidence_search import search

VERSION = 'lexical-expansion/v1'
# Authored before the held-out cases. Fixed domain vocabulary, not model calls.
ALIASES = {
    'reactions': ('interaction',), 'responsiveness': ('INP',),
    'address': ('URL',), 'exclusion': ('noindex',),
    'duplicate': ('canonical',),
}


def expanded_search(db, query):
    import re
    if not isinstance(query, str) or len(query) > 512:
        raise ValueError('Query must be text of at most 512 characters')
    terms = re.findall(r'\w+', query)
    extra = [term for word in terms for term in ALIASES.get(word.casefold(), ())]
    # Keep bounded behavior and provenance of the unchanged source passages.
    return search(db, ' '.join((terms + extra)[:20])[:512])

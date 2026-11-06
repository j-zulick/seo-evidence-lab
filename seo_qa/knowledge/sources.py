"""Curated public-source boundary. Audit stores are never publication inputs."""
from urllib.parse import urlsplit

from .contracts import Source, SourceSnapshot, RuleBinding, record_bytes, sha256


class SourceRegistry:
    def __init__(self, sources, *, aliases=None):
        self.sources = {}
        self.aliases = {}
        for source in sources:
            source = Source.model_validate(source)
            if source.id in self.sources or source.status == 'retracted':
                raise ValueError('Duplicate or retracted source')
            if urlsplit(source.canonical_url).query:
                raise ValueError('Public register forbids URL query data')
            self.sources[source.id] = source
            self.aliases[source.canonical_url] = source.id
        for alias, target in (aliases or {}).items():
            if alias in self.aliases or target not in self.sources:
                raise ValueError('Ambiguous alias or unknown source')
            self.aliases[alias] = target

    def resolve(self, key):
        return self.sources[self.aliases.get(key, key)]

    def publication_authorizer(self, approved_records, approved_blobs):
        """Trusted reviewers supply exact allowlists, after public-content review.

        Content hashes establish identity, not public provenance. Never derive
        these allowlists automatically from submitted publication content.
        """
        records = frozenset(approved_records)
        blobs = frozenset(approved_blobs)

        def authorize(operation, release, graph):
            if operation not in {'publish', 'rollback'}:
                return False
            for item in graph:
                if sha256(record_bytes(item)) not in records:
                    return False
                if isinstance(item, Source) and self.sources.get(item.id) != item:
                    return False
                if isinstance(item, SourceSnapshot) and item.availability == 'captured':
                    if {item.raw_sha256, item.text_sha256} - blobs:
                        return False
                # K10 has no executable activation authority.
                if isinstance(item, RuleBinding) and item.status == 'active_rule':
                    return False
            return True
        return authorize

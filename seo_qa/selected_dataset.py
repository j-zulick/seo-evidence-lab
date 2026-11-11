"""Explicitly selected, content-addressed local dataset; no discovery or writes."""
import json
from copy import deepcopy
from pathlib import Path
from .competitor import audit, compare, digest
from .evidence_search import index
from .release_retrieval import expanded_search, VERSION


class SelectedDataset:
    def __init__(self, payload, expected_sha256):
        if not isinstance(payload, dict) or set(payload) != {'schema', 'label', 'audits', 'passages'}:
            raise ValueError('Unsupported dataset envelope')
        if payload['schema'] != 'seo-evidence-dataset/v1' or digest(payload) != expected_sha256:
            raise ValueError('Dataset schema or hash mismatch')
        if not isinstance(payload['label'], str) or not 1 <= len(payload['label']) <= 128:
            raise ValueError('Bounded dataset label required')
        if not isinstance(payload['audits'], list) or len(payload['audits']) > 100:
            raise ValueError('At most 100 audits permitted')
        audits = {}
        for record in payload['audits']:
            if not isinstance(record, dict) or not isinstance(record.get('inputs'), dict):
                raise ValueError('Audit inputs required')
            try:
                valid = audit(record['inputs']) == record
            except (KeyError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise ValueError('Audit replay or content identity mismatch')
            if record['id'] in audits:
                raise ValueError('Duplicate audit identity')
            audits[record['id']] = deepcopy(record)
        passages = payload['passages']
        if not isinstance(passages, list) or len(passages) > 1000:
            raise ValueError('At most 1000 passages permitted')
        ids = set()
        for passage in passages:
            if not isinstance(passage, dict) or set(passage) != {'id', 'source', 'version', 'text'}:
                raise ValueError('Explicit passage provenance required')
            if any(not isinstance(v, str) or not v.strip() or len(v) > 16000 for v in passage.values()):
                raise ValueError('Bounded passage text and provenance required')
            if passage['id'] in ids:
                raise ValueError('Duplicate passage identity')
            ids.add(passage['id'])
        self._payload = deepcopy(payload)
        self._sha256 = expected_sha256
        self._audits = audits

    @property
    def payload(self):
        return deepcopy(self._payload)

    @property
    def sha256(self):
        return self._sha256

    @classmethod
    def from_file(cls, path, expected_sha256):
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 2_000_000:
            raise ValueError('Selected dataset must be a file of at most 2 MB')
        return cls(json.loads(path.read_text()), expected_sha256)

    def evidence(self, audit_id):
        if not isinstance(audit_id, str) or audit_id not in self._audits:
            raise ValueError('Unknown selected audit identity')
        return {'dataset_sha256': self.sha256, 'audit': deepcopy(self._audits[audit_id])}

    def comparison(self, before_id, after_id):
        old, new = (self.evidence(i)['audit']['inputs'] for i in (before_id, after_id))
        return {'dataset_sha256': self.sha256, 'comparison': compare(old, new)}

    def search(self, query):
        db = index(self._payload)
        try:
            return {'dataset_sha256': self.sha256, 'retrieval_version': VERSION,
                    'results': expanded_search(db, query),
                    'interpretation': 'Lexical candidates for review; no semantic truth claim.'}
        finally:
            db.close()

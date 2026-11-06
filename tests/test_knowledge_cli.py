import json
from pathlib import Path
import subprocess
import sys

from seo_qa.knowledge.contracts import RuleBinding
from seo_qa.knowledge.repository import KnowledgeRepository


def test_synthetic_knowledge_cli(tmp_path):
    audit = tmp_path / 'audit.sqlite'
    audit.write_bytes(b'audit sentinel')
    completed = subprocess.run(
        [sys.executable, '-m', 'seo_qa', 'knowledge-demo', '--state', str(audit),
         '--knowledge-dir', str(tmp_path / 'knowledge')],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True,
    )
    receipt = json.loads(completed.stdout)
    first, second = receipt['published_release_ids']
    assert receipt['mode'] == 'synthetic-local'
    assert receipt['current_before_rollback']['guidance_ids'] == ['guide:canonical:2']
    assert receipt['current_before_rollback'] == receipt['retained_after_rollback']
    assert receipt['historical_before_rollback']['guidance_ids'] == ['guide:canonical:1']
    assert receipt['historical_before_rollback'] == receipt['current_after_rollback']
    repo = KnowledgeRepository(receipt['repository'], read_only=True)
    assert repo.get_release().id == first
    assert repo.get_release(second).previous_release_id == first
    for release_id in (first, second):
        assert not any(isinstance(record, RuleBinding) for record in repo.graph(release_id)[1])
    assert receipt['citation']['quote'] == 'A canonical référence can be inspected.'
    assert audit.read_bytes() == b'audit sentinel'

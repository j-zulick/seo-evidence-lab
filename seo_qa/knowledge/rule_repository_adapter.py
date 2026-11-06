"""Read-only frozen-r4 contract adapter for synthetic local lifecycle rehearsal.

Never publishes RuleBindings or upgrades fixture attestations to real approvals.
"""
from seo_qa.knowledge.contracts import (GuidanceVersion, KnowledgeClaim, Source,
    RuleBinding, record_bytes, sha256)
from seo_qa.knowledge.repository import KnowledgeRepository
from seo_qa.knowledge.retrieval import search
from seo_qa.knowledge.freshness import assess_release
from .rule_lifecycle import digest

class RepositoryFixtureAdapter:
    def __init__(self, workflow, repository_path, context):
        self.workflow=workflow
        self.repository=KnowledgeRepository(repository_path,read_only=True)
        self.context=context

    def evidence(self, guidance_id, *, evaluated_at, policies, checks=(), metadata=()):
        if evaluated_at.date() != self.context.evaluated_on:
            raise ValueError('Assessment date must match retrieval context')
        release,records=self.repository.graph()
        if release.published_at>evaluated_at:raise ValueError('Release is future dated')
        by_id={r.id:r for r in records}
        result=search(self.repository,self.context,release_id=release.id,guidance_id=guidance_id)
        if len(result.hits)!=1:
            raise ValueError('Approved applicable guidance not found')
        guide=result.hits[0].guidance
        if not isinstance(guide,GuidanceVersion) or guide.status!='approved_guidance':
            raise ValueError('Guidance approval required')
        # Publication allows corroboration by two independent sources; executable
        # proposals additionally require primary evidence for every linked claim.
        for claim_id in guide.claim_ids:
            claim=by_id[claim_id]
            if not isinstance(claim,KnowledgeClaim) or claim.status!='corroborated' or claim.conflicts_with:
                raise ValueError('Unresolved claim')
            primary=False
            for span in claim.evidence:
                snapshot=by_id[span.snapshot_id];source=by_id[snapshot.source_id]
                primary |= isinstance(source,Source) and source.status!='retracted' and source.authority in ('official_primary','standards_primary')
            if not primary:raise ValueError('Every executable claim needs primary evidence')
        freshness=assess_release(self.repository,self.context,evaluated_at=evaluated_at,policies=policies,checks=checks,metadata=metadata)
        state=next((r for r in freshness['guidance'] if r['guidance_id']==guide.id),None)
        if freshness['release_id']!=release.id or not state or state['status']!='current':
            raise ValueError('Current digest-bound source checks and authority policy required')
        if self.repository.get_release().id!=release.id:
            raise ValueError('Current release changed during assessment')
        return release,guide,freshness

    def propose(self, rule, guidance_id, check, *, evaluated_at, policies, checks=(), metadata=(), kind='change'):
        release,guide,freshness=self.evidence(guidance_id,evaluated_at=evaluated_at,policies=policies,checks=checks,metadata=metadata)
        scoped={**rule,'knowledge_scope':guide.scope.model_dump(mode='json')}
        if guide.scope.provider is not None and scoped['providers'] != [guide.scope.provider]:
            raise ValueError('Rule provider must match guidance')
        if guide.scope.platform_version is not None and scoped['provider_versions'] != [guide.scope.platform_version]:
            raise ValueError('Rule provider version must match guidance')
        candidate=RuleBinding(id='bind:x12-fixture:'+str(int(rule['version'])),guidance_id=guide.id,check=check,
            implementation_revision='x12-fixture-v'+rule['version'],implementation_sha256=digest(scoped),status='candidate_rule_change')
        guidance=dict(release_id=release.id,release_sha256=sha256(record_bytes(release)),guidance_id=guide.id,
            status='approved',conflicts=[],release_current=True,mode='executable')
        pid=self.workflow.propose(scoped,guidance,kind=kind)
        envelope=dict(proposal=pid,fixture=True,source='verified-frozen-r4-repository',release=release.model_dump(mode='json'),
            guidance=guide.model_dump(mode='json'),candidate_binding=candidate.model_dump(mode='json'),
            freshness=freshness,mapping_semantics='UNMEASURED_FIXTURE_MAPPING',live_authority=False)
        with self.workflow.transaction() as s:
            key=self.workflow.put(s,envelope)
            self.workflow.event(s,'repository_fixture_binding',proposal=pid,envelope=key,fixture=True)
        return pid

    def activate(self,pid,*,evaluated_at,policies,checks=(),metadata=(),live=False):
        state=self.workflow.snapshot();body=state['objects'][pid];g=body['guidance']
        envelopes=[e for e in state['events'] if e['action']=='repository_fixture_binding' and e['proposal']==pid]
        if not envelopes:raise ValueError('Proposal has no repository contract binding')
        try:
            release,guide,_=self.evidence(g['guidance_id'],evaluated_at=evaluated_at,policies=policies,checks=checks,metadata=metadata)
            current=(release.id==g['release_id'] and sha256(record_bytes(release))==g['release_sha256']
                and guide.scope.model_dump(mode='json')==state['objects'][body['target']]['knowledge_scope'])
        except (ValueError,KeyError,OSError):
            current=False
        self.workflow.observe_release_fixture(g['release_sha256'],current=current,conflicts=[],status='approved' if current else 'withdrawn')
        # This remains local mock activation. No RuleBinding publication or
        # approve_rule ReviewDecision is synthesized from fixture actor strings.
        return self.workflow.activate(pid,live=live,_repository_validation=lambda: current)

"""Local synthetic rule lifecycle. No hosted adapter, network, or live authority."""
import argparse
import copy
import hashlib
import json
import math
from datetime import date
import sqlite3
from contextlib import contextmanager

SCHEMA = 'x12-local-rules/v1'

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def validate(rule):
    if set(rule) - {'knowledge_scope'} != {'rule_id', 'version', 'threshold', 'providers', 'page_types', 'provider_versions', 'exceptions', 'classification', 'remediation', 'enforcement'}:
        raise ValueError('Exact versioned mock rule fields required')
    if not all(isinstance(rule[k], str) and rule[k] for k in ('rule_id','version','classification','remediation')):
        raise ValueError('Rule identity and behavior must be explicit')
    if type(rule['threshold']) not in (int, float) or not math.isfinite(rule['threshold']) or not 0 <= rule['threshold'] <= 100:
        raise ValueError('Invalid threshold')
    for k in ('providers', 'page_types', 'provider_versions', 'exceptions'):
        if not isinstance(rule[k], list) or any(not isinstance(v, str) or not v for v in rule[k]) or len(set(rule[k])) != len(rule[k]):
            raise ValueError('Invalid applicability')
    if not all(rule[k] for k in ('providers','page_types','provider_versions')) or type(rule['enforcement']) is not bool:
        raise ValueError('Explicit applicability and enforcement required')
    if 'knowledge_scope' in rule:
        scope=rule['knowledge_scope']
        if not isinstance(scope,dict) or set(scope)!={'provider','search_engine','jurisdiction','platform_version','effective_from','effective_until','required_conditions'}:
            raise ValueError('Exact frozen applicability fields required')
        if not isinstance(scope['required_conditions'],list) or any(not isinstance(v,str) for v in scope['required_conditions']):
            raise ValueError('Explicit applicability conditions required')
        for k in ('effective_from','effective_until'):
            if scope[k] is not None:date.fromisoformat(scope[k])
    return digest(rule)

def evaluate(rule, observation):
    """Pure mock consumer; no website writes and no inference of missing measurements."""
    if 'knowledge_scope' in rule:
        scope=rule['knowledge_scope']
        try: evaluated_on=date.fromisoformat(observation['evaluated_on'])
        except (KeyError,TypeError,ValueError):return {'outcome':'unknown'}
        for key,observation_key in [('provider','provider'),('search_engine','search_engine'),('jurisdiction','jurisdiction'),('platform_version','provider_version')]:
            if scope[key] is not None and observation.get(observation_key)!=scope[key]:return {'outcome':'not_applicable'}
        conditions=observation.get('true_conditions',[])
        if not isinstance(conditions,list) or any(not isinstance(v,str) for v in conditions):return {'outcome':'unknown'}
        if not set(conditions).issuperset(scope['required_conditions']):return {'outcome':'not_applicable'}
        if scope['effective_from'] and evaluated_on<date.fromisoformat(scope['effective_from']):return {'outcome':'not_applicable'}
        if scope['effective_until'] and evaluated_on>=date.fromisoformat(scope['effective_until']):return {'outcome':'not_applicable'}
    for key, values in [('provider','providers'), ('page_type','page_types'), ('provider_version','provider_versions')]:
        if observation.get(key) not in rule[values]:
            return {'outcome':'not_applicable'}
    if observation.get('exception') in rule['exceptions']:
        return {'outcome':'not_applicable'}
    metric = observation.get('metric')
    if type(metric) not in (int, float) or not math.isfinite(metric) or not 0 <= metric <= 100:
        return {'outcome':'unknown'}
    if metric >= rule['threshold']:
        return {'outcome':'pass'}
    return {'outcome':'fail', **{k:rule[k] for k in ('classification','remediation','enforcement')}}

class Workflow:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS local_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO local_state VALUES (1, ?)', (json.dumps(dict(schema=SCHEMA, objects={}, active={}, proposals={}, audits={}, release_checks={}, events=[])),))

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            state = json.loads(self.db.execute('SELECT body FROM local_state WHERE id=1').fetchone()[0])
            if state['schema'] != SCHEMA:
                raise ValueError('Unsupported local state version')
            for key, obj in state['objects'].items():
                if digest(obj) != key:
                    raise ValueError('Immutable object integrity failure')
            yield state
            self.db.execute('UPDATE local_state SET body=? WHERE id=1', (json.dumps(state, sort_keys=True, allow_nan=False),))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    @staticmethod
    def put(s, obj):
        key = digest(obj)
        s['objects'].setdefault(key, copy.deepcopy(obj))
        return key

    @staticmethod
    def event(s, action, **data):
        s['events'].append(dict(sequence=len(s['events'])+1, action=action, **data))

    def snapshot(self):
        with self.transaction() as s:
            return copy.deepcopy(s)

    def seed_fixture(self, rule):
        key = validate(rule)
        with self.transaction() as s:
            if s['active'] or s['events']:
                raise ValueError('Seed requires empty fixture store')
            self.put(s, rule)
            s['active'][rule['rule_id']] = key
            self.event(s, 'fixture_seed', target=key, fixture=True)
        return key

    def propose(self, rule, guidance, author='fixture-author', kind='change'):
        target = validate(rule)
        if kind not in ('change','rollback') or not author.startswith('fixture-'):
            raise ValueError('Local fixture author and supported proposal kind required')
        required = {'release_id','release_sha256','guidance_id','status','conflicts','release_current','mode'}
        if set(guidance) != required or type(guidance['conflicts']) is not list or type(guidance['release_current']) is not bool or not all(isinstance(guidance[k], str) and guidance[k] for k in ('release_id','guidance_id','status','mode')) or len(guidance['release_sha256']) != 64 or any(c not in '0123456789abcdef' for c in guidance['release_sha256']):
            raise ValueError('Exact fixture guidance/release evidence required')
        with self.transaction() as s:
            base = s['active'].get(rule['rule_id'])
            if not base or base == target:
                raise ValueError('Existing rule and material new version required')
            for obj in s['objects'].values():
                if isinstance(obj, dict) and obj.get('rule_id') == rule['rule_id'] and obj.get('version') == rule['version'] and digest(obj) != target:
                    raise ValueError('Executable version is immutable')
            if kind == 'rollback' and not any(e['action'] in ('fixture_seed','activate','rollback') and e.get('target') == target for e in s['events']):
                raise ValueError('Rollback target must have been active locally')
            self.put(s, rule)
            s['release_checks'].setdefault(guidance['release_sha256'], dict(current=guidance['release_current'], conflicts=guidance['conflicts'], status=guidance['status']))
            body = dict(base=base, target=target, guidance=copy.deepcopy(guidance), author=author, kind=kind)
            pid = self.put(s, body)
            if pid not in s['proposals']:
                s['proposals'][pid] = dict(body=pid, tests=None, review=None, activated=False)
                self.event(s, 'propose', proposal=pid, fixture=True)
            return pid

    @staticmethod
    def impact_for(s, body):
        rows=[]
        for aid, pin in sorted(s['audits'].items()):
            audit=s['objects'][pin]
            if audit['rule_sha256'] != body['base']:
                continue
            before=evaluate(s['objects'][body['base']], audit['observation'])
            after=evaluate(s['objects'][body['target']], audit['observation'])
            rows.append(dict(audit_id=aid, audit_pin=pin, before=before, after=after, changed=before != after, uncertainty=after['outcome']=='unknown'))
        return dict(proposal=digest(body), base=body['base'], target=body['target'], rows=rows,
                    re_audit_candidates=[r['audit_id'] for r in rows if r['changed']],
                    unknown_candidates=[r['audit_id'] for r in rows if r['uncertainty']],
                    limitation='Only locally retained audits pinned to base; no automatic re-audit')

    def impact(self, pid):
        with self.transaction() as s:
            return self.impact_for(s, s['objects'][s['proposals'][pid]['body']])

    def audit(self, aid, observation, rule_sha256=None):
        with self.transaction() as s:
            if aid in s['audits']:
                raise ValueError('Audit IDs and pins are immutable')
            if rule_sha256 is None:
                if len(s['active']) != 1:
                    raise ValueError('Select a rule hash')
                rule_sha256=next(iter(s['active'].values()))
            if not any(e['action'] in ('fixture_seed','activate','rollback') and e.get('target') == rule_sha256 for e in s['events']):
                raise ValueError('Audit pin must reference a previously active local executable')
            rule=s['objects'][rule_sha256]
            validate(rule)
            origin=next((e for e in reversed(s['events']) if e['action'] in ('fixture_seed','activate','rollback') and e.get('target')==rule_sha256), {})
            proposal=s['objects'].get(origin.get('proposal'),{})
            result=dict(audit_id=aid, rule_sha256=rule_sha256, rule_version=rule['version'], knowledge_release_sha256=proposal.get('guidance',{}).get('release_sha256'), activation_proposal=origin.get('proposal'), observation=copy.deepcopy(observation), result=evaluate(rule, observation), fixture=True)
            pin=self.put(s, result)
            s['audits'][aid]=pin
            self.event(s, 'audit', audit_id=aid, pin=pin)
            return result

    def replay(self, aid):
        with self.transaction() as s:
            audit=copy.deepcopy(s['objects'][s['audits'][aid]])
            if evaluate(s['objects'][audit['rule_sha256']],audit['observation']) != audit['result']:
                raise ValueError('Replay mismatch')
            return audit

    def test(self, pid, cases):
        if not isinstance(cases, list) or not cases or any(set(c) != {'observation','expected'} for c in cases):
            raise ValueError('Nonempty explicit expected regression corpus required')
        with self.transaction() as s:
            p=s['proposals'][pid]
            if p['activated']:
                raise ValueError('Activated decision is final')
            body=s['objects'][p['body']]
            results=[dict(expected=c['expected'], actual=evaluate(s['objects'][body['target']],c['observation'])) for c in cases]
            att=dict(proposal=pid, suite_sha256=self.put(s,cases), passed=all(r['expected']==r['actual'] for r in results), results=results, actor='fixture-test-runner', fixture=True)
            p['tests']=self.put(s,att)
            p['review']=None  # any new test run invalidates approval
            self.event(s,'test',proposal=pid,attestation=p['tests'])
            return att

    def review(self, pid, actor, decision, reason):
        if not actor.startswith('fixture-') or decision not in ('approve','reject') or not reason.strip():
            raise ValueError('Explicit fixture reviewer, decision and reason required')
        with self.transaction() as s:
            p=s['proposals'][pid]; body=s['objects'][p['body']]
            if p['activated'] or actor==body['author'] or not p['tests']:
                raise ValueError('Separate reviewer after tests required')
            att=dict(proposal=pid, tests=p['tests'], impact_sha256=self.put(s,self.impact_for(s,body)), actor=actor, decision=decision, reason=reason, fixture=True)
            p['review']=self.put(s,att)
            self.event(s,'review',proposal=pid,attestation=p['review'])
            return att

    def observe_release_fixture(self, release_sha256, current, conflicts, status):
        """Injected release invalidation, never an observation of a real repository."""
        if type(current) is not bool or not isinstance(conflicts,list) or not isinstance(status,str):
            raise ValueError('Explicit fixture release status required')
        with self.transaction() as s:
            if release_sha256 not in s['release_checks']:
                raise ValueError('Unknown fixture release')
            check=dict(current=current,conflicts=copy.deepcopy(conflicts),status=status)
            s['release_checks'][release_sha256]=check
            self.event(s,'fixture_release_observation',release_sha256=release_sha256,check=check,fixture=True)

    @staticmethod
    def repository_binding_valid(s, pid, body, rule, event):
        """Check the exact stored envelope; an event alone is not a binding."""
        try:
            envelope=s['objects'][event['envelope']]
            release=envelope['release'];guide=envelope['guidance'];binding=envelope['candidate_binding']
            def wire_hash(value):
                return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode()).hexdigest()
            guidance=body['guidance']
            return (envelope['proposal']==pid and envelope['fixture'] is True
                and envelope['live_authority'] is False
                and release['id']==guidance['release_id']
                and wire_hash(release)==guidance['release_sha256']
                and guide['id']==guidance['guidance_id']
                and guide['status']=='approved_guidance'
                and guide['scope']==rule['knowledge_scope']
                and any(pin['id']==guide['id'] and pin['sha256']==wire_hash(guide) for pin in release['members'])
                and binding['guidance_id']==guide['id']
                and binding['status']=='candidate_rule_change'
                and binding['implementation_sha256']==body['target'])
        except (KeyError,TypeError,AttributeError,ValueError):
            return False

    def activate(self, pid, *, live=False, _repository_validation=None):
        with self.transaction() as s:
            p=s['proposals'][pid]; body=s['objects'][p['body']]; rule=s['objects'][body['target']]; g=body['guidance']
            reasons=[]
            if live: reasons.append('live_activation_disabled')
            bindings=[e for e in s['events'] if e['action']=='repository_fixture_binding' and e.get('proposal')==pid]
            bound=bool(bindings)
            # A repository-scoped proposal can survive an interruption before
            # its envelope/event commits. The rule itself must keep that residual
            # proposal behind the repository boundary, even without the event.
            repository_scoped = 'knowledge_scope' in rule or bound
            if repository_scoped and not bound:
                reasons.append('repository_binding_missing')
            if bound and not self.repository_binding_valid(s,pid,body,rule,bindings[-1]):
                reasons.append('repository_binding_invalid')
            if repository_scoped and (_repository_validation is None or _repository_validation() is not True):
                reasons.append('repository_revalidation_required')
            if p['activated']: reasons.append('decision_already_activated')
            if s['active'][rule['rule_id']] != body['base']: reasons.append('base_conflict')
            if g['status'] != 'approved': reasons.append('guidance_not_approved')
            if g['mode'] != 'executable': reasons.append('informational_only')
            if g['conflicts']: reasons.append('guidance_conflict')
            if not g['release_current']: reasons.append('stale_release')
            check=s['release_checks'][g['release_sha256']]
            if not check['current'] and 'stale_release' not in reasons: reasons.append('stale_release')
            if check['conflicts'] and 'guidance_conflict' not in reasons: reasons.append('guidance_conflict')
            if check['status'] != 'approved' and 'guidance_not_approved' not in reasons: reasons.append('guidance_not_approved')
            tests=s['objects'].get(p['tests'],{})
            review=s['objects'].get(p['review'],{})
            if not tests.get('passed'): reasons.append('regression_not_passed')
            if review.get('decision') != 'approve' or review.get('tests') != p['tests']: reasons.append('approval_missing')
            if review.get('impact_sha256') != digest(self.impact_for(s,body)): reasons.append('impact_changed_or_unreviewed')
            decision=dict(activated=not reasons, reasons=reasons, proposal=pid, fixture=True, live=False)
            self.event(s,'activation_denied' if reasons else body['kind'].replace('change','activate'), **decision, target=body['target'])
            if not reasons:
                s['active'][rule['rule_id']]=body['target']; p['activated']=True
            return decision


def fixture_rule(version='1', threshold=70):
    return dict(rule_id='mock-quality', version=version, threshold=threshold, providers=['synthetic-search'], page_types=['article'], provider_versions=['v1'], exceptions=['private-preview'], classification='needs_attention',remediation='Review the synthetic quality measure',enforcement=False)

def fixture_guidance(**changes):
    return dict(dict(release_id='fixture-release-1',release_sha256=digest({'fixture':'release-1'}),guidance_id='fixture-guidance-1',status='approved',conflicts=[],release_current=True,mode='executable'), **changes)

def fixture_observation(metric=75, **changes):
    return dict(dict(provider='synthetic-search',page_type='article',provider_version='v1',metric=metric),**changes)

def demo(path):
    w=Workflow(path)
    try:
        old=w.seed_fixture(fixture_rule())
        audit=w.audit('fixture-audit-1',fixture_observation())
        proposed=fixture_rule('2',80)
        pid=w.propose(proposed,fixture_guidance())
        blocked=w.activate(pid)
        impact=w.impact(pid)
        w.test(pid,[dict(observation=fixture_observation(), expected=evaluate(proposed,fixture_observation())),dict(observation=fixture_observation(90),expected={'outcome':'pass'})])
        w.review(pid,'fixture-reviewer','approve','Synthetic threshold impact reviewed')
        activated=w.activate(pid)
        rollback=w.propose(fixture_rule(),fixture_guidance(),kind='rollback')
        w.test(rollback,[dict(observation=fixture_observation(),expected={'outcome':'pass'})])
        w.review(rollback,'fixture-reviewer','approve','Synthetic rollback reviewed')
        rolled_back=w.activate(rollback)
        return dict(schema=SCHEMA,fixture=True,live_activation_enabled=False,initial_denial=blocked,impact=impact,activation=activated,rollback=rolled_back,historical_replay_unchanged=w.replay('fixture-audit-1')==audit,active_restored=w.snapshot()['active']['mock-quality']==old)
    finally:
        w.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['demo','inspect','apply'])
    parser.add_argument('--state',required=True,help='Isolated local fixture SQLite path; demo requires empty store')
    parser.add_argument('--request',help='JSON fixture operation file for apply')
    args=parser.parse_args()
    if args.command=='demo':
        print(json.dumps(demo(args.state),indent=2,sort_keys=True))
    else:
        w=Workflow(args.state)
        try:
            if args.command=='inspect':
                result=w.snapshot()
            else:
                if not args.request: parser.error('apply requires --request')
                with open(args.request, encoding='utf-8') as stream: request=json.load(stream)
                operation=request.pop('operation')
                if operation not in ('seed_fixture','propose','impact','audit','replay','test','review','activate','observe_release_fixture'):
                    raise ValueError('Unsupported local operation')
                result=getattr(w,operation)(**request)
            print(json.dumps(result,indent=2,sort_keys=True))
        finally: w.close()

"""Local, scheduler-callable acquisition operations; never publishes or activates rules."""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
from pathlib import Path
import sqlite3
import time

from .capture import Ingestor
from .claims import extract_claims, ClaimCandidate, verify_evidence
from .contracts import canonical_bytes, sha256, KnowledgeClaim, RuleBinding
from .sources import SourceRegistry
from .change_detection import SnapshotInput, compare_snapshots

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    daily_seconds: int = 86400
    health_seconds: int = 604800
    review_seconds: int = 2592000
    catch_up: int = 3
    stale_seconds: int = 172800

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for k, v in asdict(self).items() if k != 'enabled'):
            raise ValueError('Cadences and bounds must be positive integers')
        if type(self.enabled) is not bool:
            raise ValueError('enabled must be boolean')
        if self.catch_up > 31:
            raise ValueError('Catch-up exceeds 31 slots')


@dataclass(frozen=True)
class SourceLimits:
    timeout: float = 10
    retries: int = 2
    max_bytes: int = 2000000
    concurrency: int = 1

    def __post_init__(self):
        if (not math.isfinite(self.timeout) or not 0 < self.timeout <= 60 or
                type(self.retries) is not int or not 0 <= self.retries <= 4 or
                not 1 <= self.max_bytes <= 16000000 or self.concurrency != 1):
            raise ValueError('Unsupported source limits; acquisition is serialized per source')


class UpdateOperations:
    """A private directory holds acquisition and operation DBs plus a process lock.

    POSIX lock lifetime follows the process, so a killed worker can be restarted.
    One writer serializes all invocations, including different scheduler slot IDs.
    """
    def __init__(self, directory, sources, *, limits=None, settings=None, fixtures=None):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings = settings or Settings()
        sources = list(sources)
        if len({p.source.id for p in sources}) != len(sources):
            raise ValueError('Duplicate registry entry')
        self.registry = SourceRegistry([p.source for p in sources])
        self.sources = {p.source.id: p for p in sources}
        self.clients = {}
        self.path = self.root / 'operations.sqlite'
        with self.lock(), self.db() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {'version', 'jobs', 'tasks', 'changes', 'checks', 'events', 'candidates'}
            if tables and (not {'version', 'jobs', 'tasks', 'changes', 'checks'}.issubset(tables) or tables - expected):
                raise ValueError('Unrelated operation database')
            if tables and db.execute('SELECT value FROM version').fetchall() != [(1,)]:
                raise ValueError('Unsupported operation state version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS version (value INTEGER NOT NULL);
                INSERT INTO version SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM version);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, config TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks (job TEXT, source TEXT, baseline INTEGER, prior TEXT,
                    receipt TEXT, PRIMARY KEY(job,source));
                CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, detected REAL, reviewed REAL,
                    activated REAL, lag REAL, evidence INTEGER);
                CREATE TABLE IF NOT EXISTS checks (kind TEXT PRIMARY KEY, checked REAL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, event TEXT NOT NULL,
                    body TEXT NOT NULL, review_action TEXT, reviewed REAL, journal TEXT);
            ''')
            if db.execute('SELECT value FROM version').fetchall() != [(1,)]:
                raise ValueError('Unsupported operation state version')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Corrupt operation state')
        for key, policy in self.sources.items():
            bound = (limits or {}).get(key, SourceLimits())
            self.clients[key] = Ingestor(self.root / 'captures.sqlite', [policy],
                fixtures=fixtures, live=fixtures is None, workers=bound.concurrency,
                timeout=bound.timeout, retries=bound.retries, max_bytes=bound.max_bytes)

    @contextmanager
    def lock(self):
        with (self.root / 'writer.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def run(self, job_id):
        """Explicit manual invocation. Reuse an ID to resume, including failed receipts.

        A completed failure is retained; use a new job ID for a deliberate retry.
        """
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            raise ValueError('Invalid job identity')
        config = json.dumps({k: [asdict(self.sources[k]), c._timeout, c._retries, c._max_bytes,
                                c._mode] for k, c in sorted(self.clients.items())},
                            default=lambda v: v.model_dump(mode='json'), sort_keys=True)
        with self.lock():
            with self.db() as db:
                unfinished = db.execute('SELECT id FROM jobs WHERE done=0 AND id<>?', (job_id,)).fetchone()
                if unfinished:
                    raise ValueError('Resume incomplete job before starting another')
                db.execute('INSERT OR IGNORE INTO jobs(id,config) VALUES (?,?)', (job_id, config))
                if db.execute('SELECT config FROM jobs WHERE id=?', (job_id,)).fetchone()[0] != config:
                    raise ValueError('Job configuration changed; choose a new ID')
            for source, client in self.clients.items():
                with self.db() as db:
                    task = db.execute('SELECT baseline,prior,receipt FROM tasks WHERE job=? AND source=?',
                                      (job_id, source)).fetchone()
                    if task and task[2] is not None:
                        continue
                    if task is None:
                        with client.store.connect() as captures:
                            baseline = captures.execute('SELECT coalesce(max(id),0) FROM observations').fetchone()[0]
                        previous = client.store.previous(source, self.sources[source].method, client._mode)
                        prior = previous['snapshot_id'] if previous else None
                        db.execute('INSERT INTO tasks VALUES (?,?,?,?,NULL)', (job_id, source, baseline, prior))
                    else:
                        baseline, prior, _ = task
                # Reconcile the crash window after capture commit but before task commit.
                with client.store.connect() as captures:
                    row = captures.execute('SELECT id FROM observations WHERE id>? AND source=? AND method=? AND mode=? ORDER BY id LIMIT 1',
                        (baseline, source, self.sources[source].method, client._mode)).fetchone()
                receipt = client.store.observation(row[0])[0] if row else client.fetch(source)
                events = ()
                current = None
                if receipt['snapshot_id'] and receipt['snapshot_id'] != prior:
                    def item(key):
                        snapshot, _, text = client.store.read(key)
                        return SnapshotInput(self.sources[source].source, snapshot, text)
                    from dataclasses import replace
                    current = replace(item(receipt['snapshot_id']),
                                      observed_at=datetime.fromisoformat(receipt['observed_at']))
                    events = compare_snapshots([item(prior)] if prior else [],
                        [current], historical_complete=prior is None).events
                with self.db() as db:
                    for event in events:
                        db.execute('INSERT OR IGNORE INTO changes VALUES (?, ?,NULL,NULL,NULL,1)',
                                   (event.id, time.time()))
                        db.execute('INSERT OR IGNORE INTO events VALUES (?,?)', (event.id, event.model_dump_json()))
                        result = extract_claims([current], as_of=current.snapshot.captured_at.date(), change_event=event)
                        for candidate in result.candidates:
                            key = sha256(canonical_bytes(candidate.claim.model_dump(mode='json')))
                            db.execute('INSERT OR IGNORE INTO candidates VALUES (?,?,?,NULL,NULL,NULL)',
                                       (key, event.id, candidate.model_dump_json()))
                    db.execute('UPDATE tasks SET receipt=? WHERE job=? AND source=?',
                               (json.dumps(receipt), job_id, source))
                LOG.info('knowledge_update source_id=%s outcome=%s', source, receipt['outcome'])
            with self.db() as db:
                db.execute('UPDATE jobs SET done=1 WHERE id=?', (job_id,))
            return self.status()

    def tick(self, now=None):
        """Scheduler seam. Disabled unless explicitly configured; installs nothing."""
        if not self.settings.enabled:
            return {'schedule': 'disabled'}
        now = time.time() if now is None else now
        if not math.isfinite(now) or now < 0:
            raise ValueError('Invalid scheduler time')
        slot = int(now // self.settings.daily_seconds)
        with self.db() as db:
            last = db.execute("SELECT checked FROM checks WHERE kind='daily'").fetchone()
        start = max(0, slot - self.settings.catch_up + 1, int(last[0]) + 1 if last else slot)
        for value in range(start, slot + 1):
            self.run(f'daily:{value}')
            with self.db() as db:
                db.execute("INSERT INTO checks VALUES ('daily',?) ON CONFLICT(kind) DO UPDATE SET checked=max(checked,excluded.checked)", (value,))
        # Health/review checks are status receipts, never approvals.
        for kind, cadence in [('health', self.settings.health_seconds), ('review', self.settings.review_seconds)]:
            with self.db() as db:
                last = db.execute('SELECT checked FROM checks WHERE kind=?', (kind,)).fetchone()
                if last is None or now - last[0] >= cadence:
                    self.status(now)
                    db.execute('INSERT OR REPLACE INTO checks VALUES (?,?)', (kind, now))
        return self.status(now)

    def candidates(self):
        """Return durable K04 drafts, never approved claims."""
        with self.db() as db:
            return tuple(ClaimCandidate.model_validate_json(row[0]) for row in
                         db.execute('SELECT body FROM candidates ORDER BY id'))

    def sync_review_journal(self, journal):
        """Read exact K05 proposal claim payloads and latest decisions; never write approvals.

        Journal identity is pinned per candidate. Changing to a different plan
        requires explicit reconciliation rather than silently replacing history.
        """
        history = journal.history()
        latest = {d.proposal_id: d for d in history}
        identity = sha256(canonical_bytes(journal.plan.model_dump(mode='json')))
        by_claim = {}
        for proposal in journal.plan.proposals:
            for observation in proposal.observations:
                key = observation.claim.model_dump_json()
                if key in by_claim:
                    raise ValueError('Ambiguous claim across review proposals')
                by_claim[key] = latest.get(proposal.id)
        with self.lock(), self.db() as db:
            for key, body, pinned in db.execute('SELECT id,body,journal FROM candidates').fetchall():
                candidate = ClaimCandidate.model_validate_json(body)
                claim = candidate.claim.model_dump_json()
                if claim not in by_claim:
                    continue
                if pinned is not None and pinned != identity:
                    raise ValueError('Candidate already pinned to another review plan')
                decision = by_claim[claim]
                db.execute('UPDATE candidates SET review_action=?,reviewed=?,journal=? WHERE id=?',
                    (decision.action if decision else None,
                     decision.decided_at.timestamp() if decision else None, identity, key))
            for event, in db.execute('SELECT DISTINCT event FROM candidates').fetchall():
                pending, reviewed = db.execute(
                    "SELECT sum(review_action IS NULL OR review_action NOT IN ('approve','reject')),max(reviewed) FROM candidates WHERE event=?",
                    (event,)).fetchone()
                db.execute('UPDATE changes SET reviewed=? WHERE id=?',
                           (None if pending else reviewed, event))
        return self.status()

    def record_progress(self, event_id, *, upstream_changed_at=None, reviewed_at=None,
                        activated_at=None):
        """Record externally verified milestone times; this grants no approval."""
        values = (upstream_changed_at, reviewed_at, activated_at)
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in values):
            raise ValueError('Invalid milestone timestamp')
        with self.lock(), self.db() as db:
            row = db.execute('SELECT detected,reviewed,activated,lag FROM changes WHERE id=?',
                             (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            detected, reviewed, activated, lag = row
            if upstream_changed_at is not None:
                if upstream_changed_at > detected:
                    raise ValueError('Change timestamp follows detection')
                lag = detected - upstream_changed_at
            reviewed = reviewed_at if reviewed_at is not None else reviewed
            activated = activated_at if activated_at is not None else activated
            if reviewed is not None and reviewed < detected:
                raise ValueError('Review predates detection')
            if activated is not None and (reviewed is None or activated < reviewed):
                raise ValueError('Activation requires preceding review')
            db.execute('UPDATE changes SET reviewed=?,activated=?,lag=? WHERE id=?',
                       (reviewed, activated, lag, event_id))

    @staticmethod
    def publication_status(repository):
        """Measure the verified selected release without changing its selection."""
        release, records = repository.graph()
        by_id = {r.id: r for r in records}
        claims = [r for r in records if isinstance(r, KnowledgeClaim)]
        covered = 0
        for claim in claims:
            if not claim.evidence:
                continue
            for span in claim.evidence:
                snapshot = by_id[span.snapshot_id]
                source = by_id[snapshot.source_id]
                verify_evidence(span, source, snapshot, repository.read_blob(snapshot.text_sha256))
            covered += 1
        return {'release_id': release.id, 'claim_count': len(claims),
                'evidence_coverage': covered / len(claims) if claims else None,
                'active_binding_records': sum(isinstance(r, RuleBinding) and r.status == 'active_rule' for r in records),
                'runtime_activation_verified': False}

    def status(self, now=None):
        now = time.time() if now is None else now
        health = {}
        for source, client in self.clients.items():
            state = client.health(source)
            success = state['last_success']
            age = max(0, now - datetime.fromisoformat(success['observed_at']).timestamp()) if success else None
            health[source] = {'last_successful_fetch': success['observed_at'] if success else None,
                'age_seconds': age, 'stale': age is None or age > self.settings.stale_seconds,
                'outcome': state['latest']['outcome'] if state['latest'] else 'never_fetched'}
        with self.db() as db:
            count, event_backlog, covered = db.execute('SELECT count(*),coalesce(sum(reviewed IS NULL),0),coalesce(sum(evidence),0) FROM changes').fetchone()
            indexed = db.execute('SELECT count(*) FROM events').fetchone()[0]
            candidates, backlog = db.execute("SELECT count(*),coalesce(sum(review_action IS NULL OR review_action NOT IN ('approve','reject')),0) FROM candidates").fetchone()
            incomplete = db.execute('SELECT count(*) FROM jobs WHERE done=0').fetchone()[0]
            checks = dict(db.execute('SELECT kind,checked FROM checks'))
            detection_lag, activation_lag = db.execute(
                'SELECT avg(lag),avg(activated-reviewed) FROM changes').fetchone()
        return {'schedule_enabled_in_config': self.settings.enabled, 'scheduler_installation': 'not_managed',
                'sources': health, 'incomplete_jobs': incomplete, 'review_backlog': backlog, 'candidate_count': candidates, 'change_review_backlog': event_backlog,
                'evidence_coverage': covered / count if count else None,
                'change_detection_lag_seconds': detection_lag, 'rule_activation_lag_seconds': activation_lag,
                'unindexed_legacy_changes': count - indexed,
                'limitations': ['Lag samples require verified milestone timestamps',
                    'Evidence coverage measures captured changes, not the published corpus',
                    'Review backlog covers K11 drafts and explicitly synchronized K05 journals',
                    'Activation milestones require external verification; no activation authority'],
                'checks': checks}

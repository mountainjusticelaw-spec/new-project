"""Synthetic-only control layer. No provider clients or network effects."""
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

Opaque = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1]
    event_id: UUID
    source: Literal['synthetic.gmail']
    event_type: Literal['correspondence.received']
    firm_id: UUID
    occurred_at: datetime
    received_at: datetime
    correlation_id: UUID
    source_ref: Opaque
    source_version: Opaque
    sensitivity: Literal['synthetic']

    @field_validator('occurred_at', 'received_at', mode='before')
    @classmethod
    def timestamp_type(cls, value):
        if not isinstance(value, (datetime, str)):
            raise ValueError('timestamp must be an ISO-8601 string or datetime')  # noqa: TRY004
        return value

    @field_validator('event_id', 'firm_id', 'correlation_id')
    @classmethod
    def nonzero(cls, value):
        if not value.int:
            raise ValueError('nil UUID denied')
        return value

    @field_validator('source_ref')
    @classmethod
    def synthetic_ref(cls, value):
        if not value.startswith('synthetic-'):
            raise ValueError('synthetic source reference required')
        return value

    @field_validator('occurred_at', 'received_at')
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timezone required')
        return value.astimezone(UTC)

    @model_validator(mode='after')
    def order(self):
        if self.received_at < self.occurred_at:
            raise ValueError('receipt precedes event')
        return self

    @computed_field
    @property
    def dedup_key(self) -> str:
        # Transport IDs and receipt time are intentionally excluded.
        return digest([self.schema_version, str(self.firm_id), self.source,
                       self.event_type, self.source_ref, self.source_version])


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def canonical_signals(signals):
    """Copy exact trusted adapter signals, independent of delivery ordering."""
    if signals is None or isinstance(signals, (str, bytes)):
        raise Denied('trusted signal sequence required')
    try:
        values = tuple(signals)
    except TypeError as err:
        raise Denied('trusted signal sequence required') from err
    if any(not isinstance(value, str) or not value for value in values):
        raise Denied('invalid trusted signal')
    return tuple(sorted(set(values)))


def intake_evidence(envelope, signals, payload_sha256=None):
    """Build metadata-only evidence; transport delivery IDs are not content."""
    signals = canonical_signals(signals)
    if payload_sha256 is not None and (
        not isinstance(payload_sha256, str)
        or re.fullmatch(r'[0-9a-f]{64}', payload_sha256) is None
    ):
        raise Denied('invalid payload digest')
    metadata = envelope.model_dump(mode='json', exclude={
        'event_id', 'correlation_id', 'received_at', 'dedup_key',
    })
    envelope_sha256 = digest(metadata)
    return {
        'matter_signals': signals,
        'envelope_sha256': envelope_sha256,
        'payload_sha256': payload_sha256,
        'evidence_sha256': digest([envelope_sha256, signals, payload_sha256]),
        'source_reference': envelope.source_ref,
        'source_version': envelope.source_version,
    }


def evidence_digest(envelope, signals, payload_sha256=None):
    return intake_evidence(envelope, signals, payload_sha256)['evidence_sha256']


@dataclass(frozen=True)
class Mapping:
    firm_id: UUID
    matter_id: UUID
    clio_id: str
    eligibility: Literal['OPEN','CLOSED','RESTRICTED'] = 'OPEN'
    mapping_version: str = 'v1'


class Resolver:
    def __init__(self, mappings):
        self.mappings = tuple(mappings)

    def resolve(self, firm_id, signals):
        signals = canonical_signals(signals)
        groups = [tuple(m for m in self.mappings if m.firm_id == firm_id and m.clio_id == s)
                  for s in signals]
        matches = {m.matter_id for group in groups for m in group}
        if not matches:
            return None, 'UNMAPPED_MATTER_SIGNAL'
        if len(matches) != 1 or any(len(group) != 1 for group in groups):
            return None, 'CONFLICTING_MATTER_SIGNALS'
        if any(m.eligibility != 'OPEN' for group in groups for m in group):
            return None, 'MATTER_INELIGIBLE'
        return next(iter(matches)), 'MATTER_VERIFIED'


class State(StrEnum):
    RECEIVED='RECEIVED'
    VALIDATED='VALIDATED'
    RUNNING='RUNNING'
    REVIEW_REQUIRED='REVIEW_REQUIRED'
    APPROVED='APPROVED'
    EXECUTING='EXECUTING'
    COMPLETED='COMPLETED'
    REJECTED='REJECTED'
    FAILED='FAILED'
    QUARANTINED='QUARANTINED'
    CANCELLED='CANCELLED'
    NEEDS_RECONCILIATION='NEEDS_RECONCILIATION'


class Actor(StrEnum):
    INGRESS='INGRESS'
    RESOLVER='RESOLVER'
    COORDINATOR='COORDINATOR'
    INDEPENDENT_REVIEW='INDEPENDENT_REVIEW'
    ATTORNEY='ATTORNEY'
    POLICY_EXECUTOR='POLICY_EXECUTOR'
    AGENT='AGENT'


class Denied(RuntimeError):
    pass


# Execution-related edges are reserved in SQL; local mock-only functions exercise them.
EDGES = {
    (State.RECEIVED,State.VALIDATED):Actor.RESOLVER,
    (State.RECEIVED,State.QUARANTINED):Actor.RESOLVER,
    (State.VALIDATED,State.RUNNING):Actor.COORDINATOR,
    (State.RUNNING,State.REVIEW_REQUIRED):Actor.INDEPENDENT_REVIEW,
    (State.REVIEW_REQUIRED,State.REJECTED):Actor.ATTORNEY,
    (State.REVIEW_REQUIRED,State.APPROVED):Actor.ATTORNEY,
    (State.APPROVED,State.EXECUTING):Actor.POLICY_EXECUTOR,
    (State.EXECUTING,State.COMPLETED):Actor.POLICY_EXECUTOR,
    (State.EXECUTING,State.NEEDS_RECONCILIATION):Actor.POLICY_EXECUTOR,
    **{(s,State.CANCELLED):Actor.COORDINATOR for s in (State.VALIDATED,State.RUNNING,State.REVIEW_REQUIRED,State.APPROVED)},
    **{(s,State.FAILED):Actor.COORDINATOR for s in (State.VALIDATED,State.RUNNING)},
    **{(s,State.QUARANTINED):Actor.RESOLVER for s in (
        State.VALIDATED,State.RUNNING,State.REVIEW_REQUIRED,State.APPROVED,State.EXECUTING,
    )},
}
RESERVED = {State.APPROVED,State.EXECUTING,State.COMPLETED,State.NEEDS_RECONCILIATION}
QUARANTINE_REASONS = {'UNMAPPED_MATTER_SIGNAL','CONFLICTING_MATTER_SIGNALS',
                      'MATTER_INELIGIBLE','CONFLICTING_REPLAY_SIGNALS'}
TERMINAL = {State.QUARANTINED,State.FAILED,State.REJECTED,State.CANCELLED,
            State.COMPLETED,State.NEEDS_RECONCILIATION}


def check_edge(expected, target, actor, matter_id, reason, *, mock=False):
    if not actor or not isinstance(actor, str):
        raise Denied('actor required')
    edge = (expected, target)
    if edge not in EDGES:
        raise Denied('undefined transition')
    if (target in RESERVED and not mock) or EDGES[edge] != actor:
        raise Denied('transition not authorized in PR1')
    if target == State.QUARANTINED:
        if matter_id is not None or reason not in QUARANTINE_REASONS:
            raise Denied('invalid quarantine')
    elif target in ({State.VALIDATED,State.RUNNING,State.REVIEW_REQUIRED,State.REJECTED,State.FAILED,State.CANCELLED} | RESERVED) and matter_id is None:
        raise Denied('verified scope required')


@dataclass(frozen=True)
class Audit:
    target: State
    actor: Actor
    reason: str
    at: datetime


@dataclass(frozen=True)
class Proposal:
    run_id: UUID
    matter_id: UUID
    firm_id: UUID
    action_type: str
    destination: str
    payload: str
    source_versions: tuple[tuple[str,str], ...]
    policy_version: str
    proposal_id: UUID = field(default_factory=uuid4)

    @property
    def verified_matter_id(self) -> UUID:
        return self.matter_id

    @property
    def source_reference_versions(self) -> tuple[tuple[str,str], ...]:
        return tuple(sorted(self.source_versions))

    @property
    def action_digest(self):
        return digest([self.action_type,self.destination,self.payload,str(self.matter_id),
                       sorted(self.source_versions),self.policy_version])


ActionProposal = Proposal


@dataclass(frozen=True)
class Approval:
    run_id: UUID
    firm_id: UUID
    matter_id: UUID
    reviewer_id: UUID
    action_digest: str
    destination: str
    policy_version: str
    expires_at: datetime
    revoked: bool = False
    approval_id: UUID = field(default_factory=uuid4)
    proposal_id: UUID | None = None

    @property
    def verified_matter_id(self) -> UUID:
        return self.matter_id


@dataclass(frozen=True)
class AcceptedApproval:
    approval_id: UUID
    proposal_id: UUID
    run_id: UUID
    firm_id: UUID
    verified_matter_id: UUID
    reviewer_id: UUID
    action_digest: str
    source_reference_versions: tuple[tuple[str,str], ...]
    proposal: Proposal
    approval: Approval


@dataclass
class Run:
    firm_id: UUID
    run_id: UUID = field(default_factory=uuid4)
    status: State = State.RECEIVED
    matter_id: UUID | None = None
    audit: list[Audit] = field(default_factory=list)
    accepted_approval: AcceptedApproval | None = None
    _independently_checked: bool = field(default=False, repr=False)
    _intake: object = field(default=None, repr=False, compare=False)
    _intake_evidence: object = field(default=None, repr=False)
    _resolution_evidence: object = field(default=None, repr=False)
    _resolved_matter_id: UUID | None = field(default=None, repr=False)

    @property
    def independently_checked(self) -> bool:
        return self._independently_checked

    @property
    def intake_evidence(self):
        return self._intake_evidence

    @property
    def resolution_evidence(self):
        return self._resolution_evidence

    @property
    def resolved_matter_id(self):
        return self._resolved_matter_id

    def _require_eligibility(self, now):
        if self._intake is None or not self._intake.revalidate_run(self, now):
            raise Denied('matter is no longer eligible')

    @property
    def scope_mode(self):
        return 'MATTER_VERIFIED' if self.matter_id else 'INTAKE_RESTRICTED'

    def move(self, expected, target, actor, now, reason, *, resolved_matter=None, independently_checked=False):
        self._move(
            expected, target, actor, now, reason,
            resolved_matter=resolved_matter,
            independently_checked=independently_checked,
        )

    def _move(
        self, expected, target, actor, now, reason, *,
        resolved_matter=None, mock=False, accepted_approval=None, independently_checked=False,
    ):
        if self.status != expected:
            raise Denied('stale status')
        if resolved_matter is not None and (expected,target,actor) != (State.RECEIVED,State.VALIDATED,Actor.RESOLVER):
            raise Denied('scope change denied')
        if target == State.REVIEW_REQUIRED and independently_checked is not True:
            raise Denied('independent review required')
        if independently_checked is not False and (
            independently_checked is not True
            or (expected,target,actor) != (State.RUNNING,State.REVIEW_REQUIRED,Actor.INDEPENDENT_REVIEW)
        ):
            raise Denied('independent review binding denied')
        if target == State.APPROVED and (accepted_approval is None or self._independently_checked is not True):
            raise Denied('accepted approval and independent review required')
        if accepted_approval is not None and (
            (expected,target,actor,mock) != (State.REVIEW_REQUIRED,State.APPROVED,Actor.ATTORNEY,True)
            or self.accepted_approval is not None
        ):
            raise Denied('approval binding denied')
        matter = None if target == State.QUARANTINED else (
            resolved_matter if resolved_matter is not None else self.matter_id
        )
        check_edge(expected,target,actor,matter,reason,mock=mock)
        if self.matter_id is not None and target != State.QUARANTINED:
            self._require_eligibility(now)
        self.matter_id, self.status = matter, target
        if target == State.REVIEW_REQUIRED:
            self._independently_checked = True
        elif target == State.QUARANTINED:
            self._independently_checked = False
        if accepted_approval is not None:
            self.accepted_approval = accepted_approval
        self.audit.append(Audit(target,actor,reason,now))


class Intake:
    """Single-thread synthetic fake; not a database transaction or concurrency test."""
    def __init__(self, resolver):
        self.resolver = resolver
        self.runs = {}
        self.outbox = []
        self.steps = []
        self.agent_calls = []

    def _owned(self, run):
        if (run._intake is not self
            or not any(existing is run for existing in self.runs.values())
            or run.intake_evidence is None
            or run.intake_evidence['firm_id'] != str(run.firm_id)
            or run.intake_evidence['run_id'] != str(run.run_id)):
            raise Denied('run is not owned by this intake')

    def _quarantine(self, run, now, reason):
        if run.status in TERMINAL:
            # Preserve terminal disposition and scope while recording the anomaly.
            run.audit.append(Audit(run.status,Actor.RESOLVER,reason,now))
        else:
            run.move(run.status,State.QUARANTINED,Actor.RESOLVER,now,reason)

    def ingest_event(self, envelope, signals, *, payload_sha256=None):
        evidence = intake_evidence(envelope, signals, payload_sha256)
        key = (envelope.source,envelope.dedup_key)
        if key in self.runs:
            run = self.runs[key]
            if evidence['evidence_sha256'] != run.intake_evidence['evidence_sha256']:
                self._quarantine(run,envelope.received_at,'CONFLICTING_REPLAY_SIGNALS')
            return run, False
        run = Run(envelope.firm_id, _intake=self)
        run._intake_evidence = MappingProxyType({
            **evidence, 'inbox_id': str(uuid4()),
            'run_id': str(run.run_id), 'firm_id': str(run.firm_id),
        })
        run.audit.append(Audit(State.RECEIVED,Actor.INGRESS,'ACCEPTED',envelope.received_at))
        self.runs[key] = run
        self.outbox.append((run.run_id,'RESOLUTION_PENDING'))
        return run, True

    def resolve_run(self, run, signals=None, *, trusted_signals=None, now=None):
        self._owned(run)
        evidence = run.intake_evidence
        for supplied in (signals, trusted_signals):
            if supplied is not None and canonical_signals(supplied) != evidence['matter_signals']:
                raise Denied('resolution signals do not match immutable intake evidence')
        if run.status != State.RECEIVED or run.resolution_evidence is not None:
            raise Denied('stale resolution')
        now = now if now is not None else run.audit[0].at
        matter, reason = self.resolver.resolve(run.firm_id,evidence['matter_signals'])
        target = State.VALIDATED if matter else State.QUARANTINED
        matched = tuple(sorted(
            (m for m in self.resolver.mappings
             if m.firm_id == run.firm_id and m.clio_id in evidence['matter_signals']),
            key=lambda m: (m.clio_id,str(m.matter_id),m.mapping_version),
        ))
        snapshot = MappingProxyType({
            'run_id': str(run.run_id), 'inbox_id': evidence['inbox_id'],
            'source_reference': evidence['source_reference'],
            'source_version': evidence['source_version'],
            'extracted_signals': evidence['matter_signals'],
            'matched_clio_ids': tuple(sorted({m.clio_id for m in matched})),
            'mapping_version': MappingProxyType({m.clio_id:m.mapping_version for m in matched}),
            'decision': target.value, 'reason_code': reason,
        })
        run.move(State.RECEIVED,target,Actor.RESOLVER,now,reason,resolved_matter=matter)
        run._resolved_matter_id = matter
        run._resolution_evidence = snapshot
        return target, matter, reason

    def accept(self, envelope, signals, *, payload_sha256=None):
        run, created = self.ingest_event(envelope,signals,payload_sha256=payload_sha256)
        if created:
            self.resolve_run(run,now=envelope.received_at)
        return run, created

    def revalidate_run(self, run, now):
        self._owned(run)
        if run.status in TERMINAL or run.matter_id is None or run.resolution_evidence is None:
            return False
        matter, reason = self.resolver.resolve(run.firm_id,run.intake_evidence['matter_signals'])
        if reason != 'MATTER_VERIFIED' or matter != run.resolved_matter_id or matter != run.matter_id:
            self._quarantine(run,now,'MATTER_INELIGIBLE')
            return False
        return True

    def start(self, run, now):
        self._owned(run)
        run.move(State.VALIDATED,State.RUNNING,Actor.COORDINATOR,now,'START')
        self.steps.append((run.run_id,run.matter_id))
        self.agent_calls.append((run.run_id,'SYNTHETIC_STUB'))

    def review(self, run, now, *, independently_checked):
        self._owned(run)
        run.move(
            State.RUNNING, State.REVIEW_REQUIRED, Actor.INDEPENDENT_REVIEW,
            now, 'SYNTHETIC_REVIEW', independently_checked=independently_checked,
        )


class MockPolicyExecutor:
    """Local fixture simulation only; there is no external adapter method."""
    def __init__(self):
        self.effects = {}
        self.accepted_approvals = {}
        self.revoked_approvals = set()

    def _check(self, run, p, a, versions, now, permitted, enabled):
        if (permitted is not True or enabled is not True or a.revoked is not False
            or run.independently_checked is not True
            or not p.proposal_id.int or not a.approval_id.int or not a.reviewer_id.int
            or a.proposal_id not in (None, p.proposal_id)
            or now is None or a.expires_at.tzinfo is None or a.expires_at.utcoffset() is None
            or now.tzinfo is None or now.utcoffset() is None or a.expires_at <= now
            or run.matter_id is None
            or (run.run_id,run.firm_id,run.matter_id) != (p.run_id,p.firm_id,p.matter_id)
            or (run.run_id,run.firm_id,run.matter_id) != (a.run_id,a.firm_id,a.matter_id)
            or not p.source_versions or len({ref for ref,_ in p.source_versions}) != len(p.source_versions)
            or sorted(p.source_versions) != sorted(versions)
            or p.action_type != 'mock.review' or p.destination != 'synthetic-sink'
            or p.policy_version != 'p1'
            or (p.action_digest,p.destination,p.policy_version) != (a.action_digest,a.destination,a.policy_version)):
            raise Denied('invalid synthetic approval')

    @staticmethod
    def _materialize_versions(versions):
        if versions is None:
            raise Denied('invalid synthetic approval')
        try:
            return tuple(versions)
        except TypeError as err:
            raise Denied('invalid synthetic approval') from err

    def approve(self, run, p, a, versions, now, *, permitted=False):
        versions = self._materialize_versions(versions)
        self._check(run,p,a,versions,now,permitted,True)
        canonical_sources = tuple(sorted((ref, ver) for ref, ver in p.source_versions))
        bound_proposal = Proposal(
            p.run_id,p.matter_id,p.firm_id,p.action_type,p.destination,
            p.payload,canonical_sources,p.policy_version,p.proposal_id,
        )
        bound_approval = Approval(
            a.run_id,a.firm_id,a.matter_id,a.reviewer_id,a.action_digest,
            a.destination,a.policy_version,a.expires_at,False,a.approval_id,p.proposal_id,
        )
        accepted = AcceptedApproval(
            approval_id=a.approval_id,
            proposal_id=p.proposal_id,
            run_id=run.run_id,
            firm_id=run.firm_id,
            verified_matter_id=run.matter_id,
            reviewer_id=a.reviewer_id,
            action_digest=bound_proposal.action_digest,
            source_reference_versions=canonical_sources,
            proposal=bound_proposal,
            approval=bound_approval,
        )
        run._move(
            State.REVIEW_REQUIRED,State.APPROVED,Actor.ATTORNEY,now,'MOCK_APPROVAL',
            mock=True,accepted_approval=accepted,
        )
        self.accepted_approvals[run.run_id] = accepted

    def revoke(self, run_id: UUID, approval_id: UUID):
        self.revoked_approvals.add((run_id, approval_id))

    def execute(self, run, p=None, a=None, versions=(), now=None, *, approval_id=None, permitted=False, enabled=False, timeout=False):
        versions = self._materialize_versions(versions)
        bound = run.accepted_approval
        if bound is None or self.accepted_approvals.get(run.run_id) != bound:
            raise Denied('missing or unregistered run approval')
        if (run.run_id, bound.approval_id) in self.revoked_approvals:
            raise Denied('invalid synthetic approval')
        if approval_id is not None and approval_id != bound.approval_id:
            raise Denied('invalid synthetic approval')
        if p is None and a is None:
            if approval_id != bound.approval_id:
                raise Denied('invalid synthetic approval')
            p, a = bound.proposal, bound.approval
        elif p is None or a is None:
            raise Denied('invalid synthetic approval')
        else:
            self._check(run,p,a,versions,now,permitted,enabled)
            if (p.proposal_id != bound.proposal_id
                or a.approval_id != bound.approval_id
                or a.proposal_id not in (None, bound.proposal_id)
                or p.action_digest != bound.action_digest
                or a.action_digest != bound.action_digest
                or (p.run_id,p.firm_id,p.matter_id,p.action_type,p.destination,p.payload,tuple(sorted(p.source_versions)),p.policy_version)
                   != (bound.run_id,bound.firm_id,bound.verified_matter_id,bound.proposal.action_type,bound.proposal.destination,bound.proposal.payload,bound.source_reference_versions,bound.proposal.policy_version)
                or (a.run_id,a.firm_id,a.matter_id,a.reviewer_id,a.destination,a.policy_version,a.expires_at)
                   != (bound.run_id,bound.firm_id,bound.verified_matter_id,bound.reviewer_id,bound.approval.destination,bound.approval.policy_version,bound.approval.expires_at)):
                raise Denied('unapproved replacement action')
        self._check(run,bound.proposal,bound.approval,versions,now,permitted,enabled)
        key = (run.firm_id,bound.verified_matter_id,bound.action_digest)
        if key in self.effects:
            raise Denied('effect already leased')
        run._move(State.APPROVED,State.EXECUTING,Actor.POLICY_EXECUTOR,now,'MOCK_LEASE',mock=True)
        self.effects[key] = 'NEEDS_RECONCILIATION' if timeout else 'CONFIRMED'
        run._move(State.EXECUTING,State.NEEDS_RECONCILIATION if timeout else State.COMPLETED,
                  Actor.POLICY_EXECUTOR,now,'MOCK_TIMEOUT' if timeout else 'MOCK_COMPLETED',mock=True)

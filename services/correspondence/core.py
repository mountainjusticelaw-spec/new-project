"""Synthetic-only control layer. No provider clients or network effects."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Annotated, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, StringConstraints, computed_field, field_validator, model_validator

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
            raise ValueError('timestamp must be an ISO-8601 string or datetime')
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
        return value.astimezone(timezone.utc)

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


@dataclass(frozen=True)
class Mapping:
    firm_id: UUID
    matter_id: UUID
    clio_id: str
    eligibility: Literal['OPEN','CLOSED','RESTRICTED'] = 'OPEN'


class Resolver:
    def __init__(self, mappings):
        self.mappings = tuple(mappings)

    def resolve(self, firm_id, signals):
        signals = tuple(dict.fromkeys(signals))
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
}
RESERVED = {State.APPROVED,State.EXECUTING,State.COMPLETED,State.NEEDS_RECONCILIATION}
QUARANTINE_REASONS = {'UNMAPPED_MATTER_SIGNAL','CONFLICTING_MATTER_SIGNALS','MATTER_INELIGIBLE'}


def check_edge(expected, target, actor, matter_id, reason, *, mock=False):
    if (target in RESERVED and not mock) or EDGES.get((expected,target)) != actor:
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

    @property
    def scope_mode(self):
        return 'MATTER_VERIFIED' if self.matter_id else 'INTAKE_RESTRICTED'

    def move(self, expected, target, actor, now, reason, *, resolved_matter=None):
        self._move(expected,target,actor,now,reason,resolved_matter=resolved_matter)

    def _move(self, expected, target, actor, now, reason, *, resolved_matter=None, mock=False, accepted_approval=None):
        if self.status != expected:
            raise Denied('stale status')
        if resolved_matter is not None and (expected,target,actor) != (State.RECEIVED,State.VALIDATED,Actor.RESOLVER):
            raise Denied('scope change denied')
        if target == State.APPROVED and accepted_approval is None:
            raise Denied('accepted approval record required')
        if accepted_approval is not None and (
            (expected,target,actor,mock) != (State.REVIEW_REQUIRED,State.APPROVED,Actor.ATTORNEY,True)
            or self.accepted_approval is not None
        ):
            raise Denied('approval binding denied')
        matter = resolved_matter if resolved_matter is not None else self.matter_id
        check_edge(expected,target,actor,matter,reason,mock=mock)
        self.matter_id, self.status = matter, target
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

    def accept(self, envelope, signals):
        key = (envelope.source,envelope.dedup_key)
        if key in self.runs:
            return self.runs[key], False
        run = Run(envelope.firm_id)
        run.audit.append(Audit(State.RECEIVED,Actor.INGRESS,'ACCEPTED',envelope.received_at))
        matter, reason = self.resolver.resolve(envelope.firm_id,signals)
        run.move(State.RECEIVED,State.VALIDATED if matter else State.QUARANTINED,
                 Actor.RESOLVER,envelope.received_at,reason,resolved_matter=matter)
        self.runs[key] = run
        self.outbox.append((run.run_id,'RESOLUTION_RECORDED'))
        return run, True

    def start(self, run, now):
        run.move(State.VALIDATED,State.RUNNING,Actor.COORDINATOR,now,'START')
        self.steps.append((run.run_id,run.matter_id))
        self.agent_calls.append((run.run_id,'SYNTHETIC_STUB'))

    def review(self, run, now, *, independently_checked):
        if independently_checked is not True:
            raise Denied('independent review required')
        run.move(State.RUNNING,State.REVIEW_REQUIRED,Actor.INDEPENDENT_REVIEW,now,'SYNTHETIC_REVIEW')


class MockPolicyExecutor:
    """Local fixture simulation only; there is no external adapter method."""
    def __init__(self):
        self.effects = {}
        self.accepted_approvals = {}
        self.revoked_approvals = set()

    def _check(self, run, p, a, versions, now, permitted, enabled):
        if (permitted is not True or enabled is not True or a.revoked is not False
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

    def approve(self, run, p, a, versions, now, *, permitted=False):
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

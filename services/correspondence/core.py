"""Synthetic-only control layer. No provider clients or network effects."""
import json
import re
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from itertools import islice
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    computed_field,
    field_validator,
    model_validator,
)

Opaque = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')]
MAX_SIGNALS = 32
MAX_SIGNAL_ITEMS = 1024
MAX_REPLAY_CONFLICT_DIGESTS = 8
ISO_TIMESTAMP = re.compile(
    r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}'
    r'(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})'
)


class Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Annotated[StrictInt, Field(ge=1, le=1)]
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
        if isinstance(value, str) and ISO_TIMESTAMP.fullmatch(value) is None:
            raise ValueError('timezone-qualified ISO-8601 timestamp required')
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
        try:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError('timezone required')
            return value.astimezone(UTC)
        except (OverflowError, TypeError, ValueError) as err:
            raise ValueError('timestamp must fit the UTC datetime range') from err

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
    if signals is None or isinstance(signals, (str, bytes, MappingABC)):
        raise Denied('trusted signal sequence required')
    try:
        iterator = iter(signals)
    except TypeError as err:
        raise Denied('trusted signal sequence required') from err
    unique = set()
    for index, value in enumerate(islice(iterator, MAX_SIGNAL_ITEMS + 1)):
        if index >= MAX_SIGNAL_ITEMS:
            raise Denied('trusted signal input exceeds scan limit')
        if (type(value) is not str or not 1 <= len(value) <= 128
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
            raise Denied('invalid trusted signal')
        unique.add(value)
        if len(unique) > MAX_SIGNALS:
            raise Denied('too many distinct trusted signals')
    return tuple(sorted(unique))


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
        self.mappings = mappings

    @property
    def mappings(self):
        return self._mappings

    @mappings.setter
    def mappings(self, mappings):
        snapshot = tuple(mappings)
        index = {}
        for mapping in snapshot:
            index.setdefault((mapping.firm_id, mapping.clio_id), []).append(mapping)
        self._mappings = snapshot
        self._index = {key: tuple(group) for key, group in index.items()}

    def matches(self, firm_id, signals):
        if not isinstance(firm_id, UUID):
            raise Denied('firm UUID required')
        return tuple(
            mapping
            for signal in canonical_signals(signals)
            for mapping in self._index.get((firm_id, signal), ())
        )

    def resolve(self, firm_id, signals):
        signals = canonical_signals(signals)
        if not isinstance(firm_id, UUID):
            raise Denied('firm UUID required')
        groups = [self._index.get((firm_id, signal), ()) for signal in signals]
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


def validate_transition(expected, target, actor):
    """Accept enum members or exact enum-value strings before hashing input."""
    normalized = []
    for value, enum_type in ((expected, State), (target, State), (actor, Actor)):
        if isinstance(value, enum_type):
            normalized.append(value)
        elif type(value) is str:
            try:
                normalized.append(enum_type(value))
            except ValueError as err:
                raise Denied('invalid transition state or actor') from err
        else:
            raise Denied('invalid transition state or actor')
    return tuple(normalized)


def check_edge(expected, target, actor, matter_id, reason, *, mock=False):
    expected, target, actor = validate_transition(expected, target, actor)
    if type(mock) is not bool:
        raise Denied('invalid mock flag')
    edge = (expected, target)
    if edge not in EDGES:
        raise Denied('undefined transition')
    if (target in RESERVED and not mock) or EDGES[edge] != actor:
        raise Denied('transition not authorized in PR1')
    if target == State.QUARANTINED:
        if matter_id is not None or type(reason) is not str or reason not in QUARANTINE_REASONS:
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
    _executor: object = field(default=None, repr=False, compare=False)
    _leased_effect_digest: str | None = field(default=None, repr=False)
    replay_conflict_count: int = 0
    replay_conflict_digests: tuple[str, ...] = ()

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
        if not isinstance(self._intake, Intake) or not self._intake.revalidate_run(self, now):
            raise Denied('matter is no longer eligible')

    @property
    def scope_mode(self):
        return 'MATTER_VERIFIED' if self.matter_id else 'INTAKE_RESTRICTED'

    def _verify_resolved_scope(self):
        evidence = self._resolution_evidence
        intake_ev = self._intake_evidence
        if (
            self.matter_id is None
            or self._resolved_matter_id is None
            or self.matter_id != self._resolved_matter_id
            or not isinstance(evidence, MappingProxyType)
            or not isinstance(evidence.get('mapping_version'), MappingProxyType)
            or not isinstance(intake_ev, MappingProxyType)
            or evidence.get('run_id') != str(self.run_id)
            or evidence.get('inbox_id') != intake_ev.get('inbox_id')
            or evidence.get('source_reference') != intake_ev.get('source_reference')
            or evidence.get('source_version') != intake_ev.get('source_version')
            or evidence.get('extracted_signals') != intake_ev.get('matter_signals')
            or evidence.get('matched_clio_ids') != intake_ev.get('matter_signals')
            or not evidence.get('matched_clio_ids')
            or evidence.get('decision') != State.VALIDATED.value
            or evidence.get('reason_code') != 'MATTER_VERIFIED'
        ):
            raise Denied('verified resolution evidence required')

    def _verify_initial_resolution(
        self, target, resolved_matter, reason, resolution_evidence, intake_token,
    ):
        intake = self._intake
        if (
            not isinstance(intake, Intake)
            or intake_token is not intake._auth_token
            or self.matter_id is not None
            or self._resolved_matter_id is not None
            or self._resolution_evidence is not None
            or not isinstance(resolution_evidence, MappingProxyType)
            or not isinstance(resolution_evidence.get('mapping_version'), MappingProxyType)
        ):
            raise Denied('verified resolution evidence required')
        exp_target, exp_matter, exp_reason, exp_snapshot = intake._derive_resolution(self)
        if (target, resolved_matter, reason) != (exp_target, exp_matter, exp_reason):
            raise Denied('resolution mismatches bound intake evidence')
        keys = set(exp_snapshot.keys())
        if (
            set(resolution_evidence.keys()) != keys
            or any(
                resolution_evidence[k] != exp_snapshot[k]
                for k in keys - {'mapping_version'}
            )
            or dict(resolution_evidence['mapping_version'])
            != dict(exp_snapshot['mapping_version'])
        ):
            raise Denied('invalid resolution evidence snapshot')
        if target == State.VALIDATED:
            matched_ids = resolution_evidence['matched_clio_ids']
            if exp_matter is None or not matched_ids:
                raise Denied('verified matter mapping required')
            for clio_id in matched_ids:
                matches = intake.resolver.matches(self.firm_id, (clio_id,))
                if (
                    len(matches) != 1
                    or matches[0].matter_id != exp_matter
                    or matches[0].eligibility != 'OPEN'
                ):
                    raise Denied('open matter mapping required')
        return exp_matter

    def _verify_safety_quarantine(
        self, expected, reason, resolved_matter, resolution_evidence, intake_token,
    ):
        intake = self._intake
        if (
            not isinstance(intake, Intake)
            or intake_token is not intake._auth_token
            or resolved_matter is not None
            or resolution_evidence is not None
        ):
            raise Denied('direct quarantine denied')
        if reason == 'CONFLICTING_REPLAY_SIGNALS':
            if self.run_id not in intake._conflicted_runs:
                raise Denied('unverified conflicting replay quarantine')
            return
        if expected == State.RECEIVED or reason != 'MATTER_INELIGIBLE':
            raise Denied('invalid safety quarantine')
        self._verify_resolved_scope()
        intake_ev = self._intake_evidence
        if not isinstance(intake_ev, MappingProxyType):
            raise Denied('verified intake evidence required')
        current_matter, current_reason = intake.resolver.resolve(
            self.firm_id, intake_ev['matter_signals'],
        )
        if (current_matter, current_reason) == (self._resolved_matter_id, 'MATTER_VERIFIED'):
            raise Denied('matter remains eligible')

    def _verify_executor_edge(
        self, expected, target, now, accepted_approval, leased_digest, executor, executor_token,
    ):
        if (
            not isinstance(executor, MockPolicyExecutor)
            or executor_token is not executor._auth_token
            or self._independently_checked is not True
        ):
            raise Denied('policy executor authorization required')
        if target == State.APPROVED:
            if (
                expected != State.REVIEW_REQUIRED
                or self.accepted_approval is not None
                or self._executor is not None
                or leased_digest is not None
                or not isinstance(accepted_approval, AcceptedApproval)
                or (
                    accepted_approval.run_id,
                    accepted_approval.firm_id,
                    accepted_approval.verified_matter_id,
                ) != (self.run_id, self.firm_id, self.matter_id)
                or accepted_approval.proposal_id != accepted_approval.proposal.proposal_id
                or accepted_approval.approval_id != accepted_approval.approval.approval_id
                or accepted_approval.action_digest != accepted_approval.proposal.action_digest
                or accepted_approval.action_digest != accepted_approval.approval.action_digest
                or accepted_approval.approval.revoked is not False
                or accepted_approval.approval.expires_at <= now
                or (self.run_id, accepted_approval.approval_id) in executor.revoked_approvals
            ):
                raise Denied('accepted approval binding denied')
            return
        bound = self.accepted_approval
        if (
            bound is None
            or accepted_approval is not None
            or self._executor is not executor
            or executor.accepted_approvals.get(self.run_id) != bound
            or (self.run_id, bound.approval_id) in executor.revoked_approvals
            or (bound.run_id, bound.firm_id, bound.verified_matter_id)
            != (self.run_id, self.firm_id, self.matter_id)
            or leased_digest != bound.action_digest
        ):
            raise Denied('bound approval lease required')
        effect_key = (self.firm_id, bound.verified_matter_id, bound.action_digest)
        if target == State.EXECUTING:
            if (
                expected != State.APPROVED
                or self._leased_effect_digest is not None
                or bound.approval.expires_at <= now
                or effect_key in executor.effects
            ):
                raise Denied('execution lease denied')
            return
        expected_outcome = (
            'NEEDS_RECONCILIATION' if target == State.NEEDS_RECONCILIATION else 'CONFIRMED'
        )
        if (
            expected != State.EXECUTING
            or self._leased_effect_digest != bound.action_digest
            or executor.effects.get(effect_key) != expected_outcome
        ):
            raise Denied('execution completion requires active lease')

    def move(
        self, expected, target, actor, now, reason, *,
        resolved_matter=None, independently_checked=False,
    ):
        self._move(
            expected, target, actor, now, reason,
            resolved_matter=resolved_matter,
            independently_checked=independently_checked,
        )

    def _move(
        self, expected, target, actor, now, reason, *,
        resolved_matter=None, mock=False, accepted_approval=None,
        independently_checked=False, resolution_evidence=None,
        safety_quarantine=False, leased_digest=None,
        _intake_token=None, _executor=None, _executor_token=None,
    ):
        expected, target, actor = validate_transition(expected, target, actor)
        if type(mock) is not bool or type(safety_quarantine) is not bool:
            raise Denied('invalid transition flag')
        if self.status != expected:
            raise Denied('stale status')
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise Denied('utc timestamp required')
        if not isinstance(reason, str) or not reason.strip():
            raise Denied('reason required')
        if (
            resolved_matter is not None
            and (expected, target, actor) != (State.RECEIVED, State.VALIDATED, Actor.RESOLVER)
        ):
            raise Denied('scope change denied')
        review_edge = (
            expected, target, actor
        ) == (State.RUNNING, State.REVIEW_REQUIRED, Actor.INDEPENDENT_REVIEW)
        if (expected == State.RECEIVED or target == State.QUARANTINED) and (
            independently_checked is not False
        ):
            raise Denied('independent review binding denied')
        if resolution_evidence is not None and (
            expected != State.RECEIVED
            or target not in {State.VALIDATED, State.QUARANTINED}
            or actor != Actor.RESOLVER
            or safety_quarantine is not False
        ):
            raise Denied('resolution evidence binding denied')
        if accepted_approval is not None and (
            (expected, target, actor, mock)
            != (State.REVIEW_REQUIRED, State.APPROVED, Actor.ATTORNEY, True)
        ):
            raise Denied('approval binding denied')
        if leased_digest is not None and (expected, target, actor, mock) not in {
            (State.APPROVED, State.EXECUTING, Actor.POLICY_EXECUTOR, True),
            (State.EXECUTING, State.COMPLETED, Actor.POLICY_EXECUTOR, True),
            (State.EXECUTING, State.NEEDS_RECONCILIATION, Actor.POLICY_EXECUTOR, True),
        }:
            raise Denied('lease binding denied')

        matter = None if target == State.QUARANTINED else (
            resolved_matter if expected == State.RECEIVED else self.matter_id
        )
        check_edge(expected, target, actor, matter, reason, mock=mock)
        if not isinstance(self._intake, Intake):
            raise Denied('run is not owned by this intake')
        self._intake._owned(self)

        if expected == State.RECEIVED and safety_quarantine is not True:
            matter = self._verify_initial_resolution(
                target, resolved_matter, reason, resolution_evidence, _intake_token,
            )
            self.matter_id = matter
            self._resolved_matter_id = matter
            self._resolution_evidence = resolution_evidence
            self.status = target
            self.audit.append(Audit(target, actor, reason, now))
            return

        if target == State.QUARANTINED:
            if safety_quarantine is not True:
                raise Denied('direct quarantine denied')
            self._verify_safety_quarantine(
                expected, reason, resolved_matter, resolution_evidence, _intake_token,
            )
            self.matter_id = None
            self.status = State.QUARANTINED
            self._independently_checked = False
            self._leased_effect_digest = None
            self.audit.append(Audit(target, actor, reason, now))
            return

        self._verify_resolved_scope()
        if expected in {State.REVIEW_REQUIRED, State.APPROVED, State.EXECUTING} and (
            self._independently_checked is not True
        ):
            raise Denied('independent review required')
        if expected == State.APPROVED and self.accepted_approval is None:
            raise Denied('accepted approval required')
        if target in RESERVED:
            self._verify_executor_edge(
                expected, target, now, accepted_approval,
                leased_digest, _executor, _executor_token,
            )

        self._require_eligibility(now)
        if review_edge and independently_checked is not True:
            raise Denied('independent review required')
        if not review_edge and independently_checked is not False:
            raise Denied('independent review binding denied')
        self.matter_id = matter
        self.status = target
        if target == State.REVIEW_REQUIRED:
            self._independently_checked = True
        if accepted_approval is not None:
            self.accepted_approval = accepted_approval
            self._executor = _executor
        if target == State.EXECUTING:
            self._leased_effect_digest = leased_digest
        self.audit.append(Audit(target, actor, reason, now))


class Intake:
    """Single-thread synthetic fake; not a database transaction or concurrency test."""
    def __init__(self, resolver):
        self.resolver = resolver
        self.runs = {}
        self._event_runs = {}
        self.outbox = []
        self.steps = []
        self.agent_calls = []
        self._auth_token = object()
        self._conflicted_runs: set[UUID] = set()

    def _owned(self, run):
        if (not isinstance(run, Run)
            or not isinstance(run.run_id, UUID)
            or run._intake is not self
            or self.runs.get(run.run_id) is not run
            or not isinstance(run.intake_evidence, MappingProxyType)
            or run.intake_evidence.get('firm_id') != str(run.firm_id)
            or run.intake_evidence.get('run_id') != str(run.run_id)):
            raise Denied('run is not owned by this intake')

    def _derive_resolution(self, run):
        self._owned(run)
        evidence = run.intake_evidence
        signals = canonical_signals(evidence['matter_signals'])
        if signals != evidence['matter_signals']:
            raise Denied('invalid bound intake signals')
        matter, reason = self.resolver.resolve(run.firm_id, signals)
        target = State.VALIDATED if matter else State.QUARANTINED
        matched = tuple(sorted(
            self.resolver.matches(run.firm_id, signals),
            key=lambda m: (m.clio_id, str(m.matter_id), m.mapping_version),
        ))
        snapshot = MappingProxyType({
            'run_id': str(run.run_id),
            'inbox_id': evidence['inbox_id'],
            'source_reference': evidence['source_reference'],
            'source_version': evidence['source_version'],
            'extracted_signals': signals,
            'matched_clio_ids': tuple(sorted({m.clio_id for m in matched})),
            'mapping_version': MappingProxyType({m.clio_id: m.mapping_version for m in matched}),
            'decision': target.value,
            'reason_code': reason,
        })
        return target, matter, reason, snapshot

    def _quarantine(self, run, now, reason):
        self._owned(run)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise Denied('utc timestamp required')
        if run.status in TERMINAL:
            # Preserve terminal disposition and scope while recording the anomaly.
            run.audit.append(Audit(run.status, Actor.RESOLVER, reason, now))
        else:
            run._move(
                run.status, State.QUARANTINED, Actor.RESOLVER, now, reason,
                safety_quarantine=True, _intake_token=self._auth_token,
            )

    def ingest_event(self, envelope, signals, *, payload_sha256=None):
        evidence = intake_evidence(envelope, signals, payload_sha256)
        key = (envelope.source, envelope.dedup_key)
        if key in self._event_runs:
            run = self.runs[self._event_runs[key]]
            self._owned(run)
            if evidence['evidence_sha256'] != run.intake_evidence['evidence_sha256']:
                run.replay_conflict_count += 1
                conflict_digest = evidence['evidence_sha256']
                if (conflict_digest not in run.replay_conflict_digests
                    and len(run.replay_conflict_digests) < MAX_REPLAY_CONFLICT_DIGESTS):
                    run.replay_conflict_digests += (conflict_digest,)
                    self._conflicted_runs.add(run.run_id)
                    try:
                        self._quarantine(run, envelope.received_at, 'CONFLICTING_REPLAY_SIGNALS')
                    finally:
                        self._conflicted_runs.discard(run.run_id)
            elif run.status == State.RECEIVED:
                self.resolve_run(run, now=envelope.received_at)
            elif run.status not in TERMINAL:
                self.revalidate_run(run, envelope.received_at)
            return run, False
        run = Run(envelope.firm_id, _intake=self)
        run._intake_evidence = MappingProxyType({
            **evidence, 'inbox_id': str(uuid4()),
            'run_id': str(run.run_id), 'firm_id': str(run.firm_id),
        })
        run.audit.append(Audit(State.RECEIVED, Actor.INGRESS, 'ACCEPTED', envelope.received_at))
        self.runs[run.run_id] = run
        self._event_runs[key] = run.run_id
        self.outbox.append((run.run_id, 'RESOLUTION_PENDING'))
        return run, True

    def resolve_run(self, run, signals=None, *, trusted_signals=None, now=None):
        self._owned(run)
        evidence = run.intake_evidence
        for supplied in (signals, trusted_signals):
            if supplied is not None and canonical_signals(supplied) != evidence['matter_signals']:
                raise Denied('resolution signals do not match immutable intake evidence')
        if (
            run.status != State.RECEIVED
            or run.matter_id is not None
            or run.resolved_matter_id is not None
            or run.resolution_evidence is not None
        ):
            raise Denied('stale resolution')
        now = now if now is not None else run.audit[0].at
        target, matter, reason, snapshot = self._derive_resolution(run)
        run._move(
            State.RECEIVED, target, Actor.RESOLVER, now, reason,
            resolved_matter=matter, resolution_evidence=snapshot,
            _intake_token=self._auth_token,
        )
        return target, matter, reason

    def accept(self, envelope, signals, *, payload_sha256=None):
        run, created = self.ingest_event(envelope, signals, payload_sha256=payload_sha256)
        if run.status == State.RECEIVED:
            self.resolve_run(run, now=envelope.received_at)
        return run, created

    def revalidate_run(self, run, now):
        self._owned(run)
        if run.status in TERMINAL or run.matter_id is None or run.resolution_evidence is None:
            return False
        matter, reason = self.resolver.resolve(run.firm_id, run.intake_evidence['matter_signals'])
        if (
            reason != 'MATTER_VERIFIED'
            or matter != run.resolved_matter_id
            or matter != run.matter_id
        ):
            self._quarantine(run, now, 'MATTER_INELIGIBLE')
            return False
        return True

    def start(self, run, now):
        self._owned(run)
        run.move(State.VALIDATED, State.RUNNING, Actor.COORDINATOR, now, 'START')
        self.steps.append((run.run_id, run.matter_id))
        self.agent_calls.append((run.run_id, 'SYNTHETIC_STUB'))

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
        self._auth_token = object()

    def _check(self, run, p, a, versions, now, permitted, enabled):
        if (permitted is not True or enabled is not True or a.revoked is not False
            or run.independently_checked is not True
            or not p.proposal_id.int or not a.approval_id.int or not a.reviewer_id.int
            or a.proposal_id not in (None, p.proposal_id)
            or now is None or a.expires_at.tzinfo is None or a.expires_at.utcoffset() is None
            or now.tzinfo is None or now.utcoffset() is None or a.expires_at <= now
            or run.matter_id is None
            or (run.run_id, run.firm_id, run.matter_id) != (p.run_id, p.firm_id, p.matter_id)
            or (run.run_id, run.firm_id, run.matter_id) != (a.run_id, a.firm_id, a.matter_id)
            or not p.source_versions
            or len({ref for ref, _ in p.source_versions}) != len(p.source_versions)
            or sorted(p.source_versions) != sorted(versions)
            or p.action_type != 'mock.review' or p.destination != 'synthetic-sink'
            or p.policy_version != 'p1'
            or (p.action_digest, p.destination, p.policy_version)
            != (a.action_digest, a.destination, a.policy_version)):
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
        if (run.run_id, a.approval_id) in self.revoked_approvals:
            raise Denied('invalid synthetic approval')
        self._check(run, p, a, versions, now, permitted, True)
        canonical_sources = tuple(sorted((ref, ver) for ref, ver in p.source_versions))
        bound_proposal = Proposal(
            p.run_id, p.matter_id, p.firm_id, p.action_type, p.destination,
            p.payload, canonical_sources, p.policy_version, p.proposal_id,
        )
        bound_approval = Approval(
            a.run_id, a.firm_id, a.matter_id, a.reviewer_id, a.action_digest,
            a.destination, a.policy_version, a.expires_at, False, a.approval_id, p.proposal_id,
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
            State.REVIEW_REQUIRED, State.APPROVED, Actor.ATTORNEY, now, 'MOCK_APPROVAL',
            mock=True, accepted_approval=accepted,
            _executor=self, _executor_token=self._auth_token,
        )
        self.accepted_approvals[run.run_id] = accepted

    def revoke(self, run_id: UUID, approval_id: UUID):
        self.revoked_approvals.add((run_id, approval_id))

    def execute(
        self, run, p=None, a=None, versions=(), now=None, *,
        approval_id=None, permitted=False, enabled=False, timeout=False,
    ):
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
            self._check(run, p, a, versions, now, permitted, enabled)
            bound_proposal_tuple = (
                bound.run_id, bound.firm_id, bound.verified_matter_id,
                bound.proposal.action_type, bound.proposal.destination,
                bound.proposal.payload, bound.source_reference_versions,
                bound.proposal.policy_version,
            )
            supplied_proposal_tuple = (
                p.run_id, p.firm_id, p.matter_id, p.action_type, p.destination,
                p.payload, tuple(sorted(p.source_versions)), p.policy_version,
            )
            bound_approval_tuple = (
                bound.run_id, bound.firm_id, bound.verified_matter_id,
                bound.reviewer_id, bound.approval.destination,
                bound.approval.policy_version, bound.approval.expires_at,
            )
            supplied_approval_tuple = (
                a.run_id, a.firm_id, a.matter_id, a.reviewer_id,
                a.destination, a.policy_version, a.expires_at,
            )
            if (
                p.proposal_id != bound.proposal_id
                or a.approval_id != bound.approval_id
                or a.proposal_id not in (None, bound.proposal_id)
                or p.action_digest != bound.action_digest
                or a.action_digest != bound.action_digest
                or supplied_proposal_tuple != bound_proposal_tuple
                or supplied_approval_tuple != bound_approval_tuple
            ):
                raise Denied('unapproved replacement action')
        self._check(run, bound.proposal, bound.approval, versions, now, permitted, enabled)
        key = (run.firm_id, bound.verified_matter_id, bound.action_digest)
        if key in self.effects:
            raise Denied('effect already leased')
        run._move(
            State.APPROVED, State.EXECUTING, Actor.POLICY_EXECUTOR, now, 'MOCK_LEASE',
            mock=True, leased_digest=bound.action_digest,
            _executor=self, _executor_token=self._auth_token,
        )
        self.effects[key] = 'NEEDS_RECONCILIATION' if timeout else 'CONFIRMED'
        run._move(
            State.EXECUTING,
            State.NEEDS_RECONCILIATION if timeout else State.COMPLETED,
            Actor.POLICY_EXECUTOR,
            now,
            'MOCK_TIMEOUT' if timeout else 'MOCK_COMPLETED',
            mock=True,
            leased_digest=bound.action_digest,
            _executor=self,
            _executor_token=self._auth_token,
        )

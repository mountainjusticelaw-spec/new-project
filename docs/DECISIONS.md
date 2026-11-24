# Architecture decisions

Proposed baseline decisions, subject to review and revision:
- ADR-001: Private monorepo. Simplifies shared contracts and coordinated changes; revisit if independent deployment or access boundaries require split repositories.
- ADR-002: Google Cloud-first, ADK preferred, portable interfaces. Managed services reduce operations burden; mitigate lock-in with provider adapters, PostgreSQL and export/restore testing. Alternative: n8n/LangGraph or another provider when justified.
- ADR-003: Deterministic workflow state and policy outside agents. Prevents probabilistic authorization; adds engineering cost but enables audit and replay.
- ADR-004: Authoritative originals remain in existing systems. Avoid unnecessary replication; track source versions and validate authorized retrieval. Any derived copies require retention review.
- ADR-005: Documentation-only foundation; no deployments or data connections. Local synthetic vertical slice precedes cloud provisioning.

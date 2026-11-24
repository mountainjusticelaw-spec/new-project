# WeaverLawAgents

Engineering repository for WeaverLawOS, an attorney-supervised, event-driven law-office AI operating system.

**Status:** v0.1.0 Foundation (documentation and scaffolding only). No agents, integrations, cloud resources or production controls are implemented or deployed by this baseline.

## Mission
Convert authorized office events into evidence-backed work products and reviewable proposed actions. Preserve attorney judgment, confidentiality, authoritative case records and a complete audit trail.

## Start here
- [AGENTS.md](AGENTS.md): instructions for developers and coding assistants.
- [ARCHITECTURE.md](ARCHITECTURE.md): proposed Google Cloud architecture.
- [SECURITY.md](SECURITY.md): mandatory design and release controls.
- [ROADMAP.md](ROADMAP.md): staged implementation and acceptance gates.
- [Agent standard](docs/AGENT_STANDARD.md), [workflow standard](docs/WORKFLOW_STANDARD.md), [governance](docs/GOVERNANCE.md).
- [First workflow](workflows/incoming-email/README.md): synthetic incoming correspondence review.

## Technology direction
Python, Google ADK and Gemini via Google Cloud; Cloud Run, Pub/Sub, Scheduler, Cloud SQL/PostgreSQL, Secret Manager; a future Next.js/TypeScript command center. LangGraph or alternate model providers are optional adapters, not baseline dependencies.

## Data and development
Clio is the intended authoritative matter record; Dropbox is the intended original-document repository. GitHub stores software and non-client-specific templates only. Do not add client information, secrets, live transcripts, credentials, or case files. Use wholly synthetic fixtures. No installation or deployment command is required to review this baseline.

All external connections, cloud provisioning, production access and real-data processing require separate authorization and validated controls. No open-source license is granted by this repository baseline.

# WeaverLawAgents

Engineering repository for WeaverLawOS, an attorney-supervised, event-driven law-office AI operating system.

**Status:** Local synthetic correspondence control layer. Deterministic Python controls, an in-memory mock executor, and disposable PostgreSQL tests are implemented. Model runtimes, live integrations, cloud resources, and production deployment remain disabled.

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
The locked future stack is Python, Google ADK, Gemini, and Cloud SQL for PostgreSQL; see [architecture decisions](docs/DECISIONS.md). Cloud Run, Pub/Sub, Scheduler, Secret Manager, and a Next.js/TypeScript command center remain future components. PR1 activates no model runtime or cloud service.

## Data and development
Clio is the intended authoritative matter record; Dropbox is the intended original-document repository. GitHub stores software and non-client-specific templates only. Do not add client information, secrets, live transcripts, credentials, or case files. Use wholly synthetic fixtures. No installation or deployment command is required to review this baseline.

All external connections, cloud provisioning, production access and real-data processing require separate authorization and validated controls. No open-source license is granted by this repository baseline.

# Repository skills

These ten workspace skills describe the authorized synthetic correspondence control layer. They are repository-authored implementations of the checked-in architecture, governance, workflow, and PR1 contracts; they do not claim to reproduce the unavailable owner source document verbatim.

| Skill | Use |
| --- | --- |
| [WL-01 Scope the vertical slice](../.agents/skills/wl-01-scope-vertical-slice/SKILL.md) | Keep correspondence-control changes within the authorized PR1 synthetic slice and locked stack. |
| [WL-02 Trusted metadata ingress](../.agents/skills/wl-02-trusted-metadata-ingress/SKILL.md) | Validate metadata-only synthetic ingress and bind canonical source evidence at acceptance. |
| [WL-03 Evidence-bound matter resolution](../.agents/skills/wl-03-matter-resolution-evidence/SKILL.md) | Resolve exact trusted matter signals from immutable inbox evidence and continuously recheck eligibility. |
| [WL-04 Source provenance](../.agents/skills/wl-04-source-provenance/SKILL.md) | Preserve immutable source references, versions, and evidence digests without copying client content. |
| [WL-05 Deterministic workflow controls](../.agents/skills/wl-05-deterministic-workflow-controls/SKILL.md) | Enforce explicit actor/state edges, current matter scope, and atomic denial behavior. |
| [WL-06 Independent review](../.agents/skills/wl-06-independent-review/SKILL.md) | Keep evidence review separate from coordination and preserve attorney authority. |
| [WL-07 Exact-action approvals](../.agents/skills/wl-07-exact-action-approvals/SKILL.md) | Bind local simulated effects to immutable accepted approvals and revalidate every execution condition. |
| [WL-08 Audit and RLS isolation](../.agents/skills/wl-08-audit-rls-isolation/SKILL.md) | Verify forced row isolation, immutable evidence, and isolated transactional audit writes. |
| [WL-09 Replay and recovery](../.agents/skills/wl-09-replay-recovery/SKILL.md) | Handle identical and contradictory replays without duplicate runs or unauthorized automatic retries. |
| [WL-10 Synthetic fixtures and gates](../.agents/skills/wl-10-synthetic-fixtures-gates/SKILL.md) | Run deterministic unit and real disposable PostgreSQL release gates using fictional fixtures. |

Each directory contains `SKILL.md` and `agents/openai.yaml` for local discovery. Begin with WL-01; apply the other skills when their control is relevant. WL-03, WL-05, WL-08, WL-09, and WL-10 cover the evidence, eligibility, isolation, replay, and verification boundaries exercised by PR1.

Skills remain subordinate to the user's authorized task, [AGENTS.md](../AGENTS.md), security policy, and attorney authority. They do not enable ADK/Gemini execution, live integrations, external effects, infrastructure, or deployment. Changes to skill guidance must preserve synthetic-only data and deterministic authorization.

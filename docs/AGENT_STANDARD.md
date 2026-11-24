# Agent standard

Every agent is a versioned, bounded worker, not an autonomous lawyer. ADK is preferred; provider adapters must keep policy and business logic independent.

## Required specification
- Stable agent_id, version, role, accountable owner, purpose, non-goals and reporting relationship.
- Input/output schema version; verified matter scope; required source classes and fresh retrieval; allowed data classifications.
- Model/provider configuration, prompt version, allowed tools and explicit prohibited capabilities.
- Execution limits (time, steps, tokens/cost, retry policy), deterministic stopping rules, abstention and escalation conditions.
- Required evidence references, uncertainty, assumptions, conflict/missing-source flags, proposed actions (never executed by agent), review requirements.
- Evaluations and release approval with rollback version.

Recommended directory: agent.yaml, instructions.md, tools.py, permissions.yaml, output_schema.json, evaluations/, README.md. These are future implementation files, not active configuration today.

## Execution rules
Agents receive a scope-bound task and minimal context. A coordinator authorizes delegation; delegated workers cannot expand access. Keep short-lived run context separate from persistent matter knowledge; no unscoped memory. Validate output independently; reject malformed outputs and fabricated sources. Mark unverified citations and legal deadlines; retrieve current primary authorities and subsequent treatment using licensed/authorized tools. No conclusion based on inaccessible evidence. Cite document ID/version, page or location, exact support when appropriate; preserve original and extraction gaps. Distinguish fact, allegation, inference and recommendation.

## Review
Independent reviewer receives source material and draft, checks adverse authority, factual support, dates, missing evidence and scope; never treats another agent's conclusion as proof. Attorney alone approves legal decisions and external actions. Record reviewer findings and resolution.

## Minimum evaluations
Normal representative task, ambiguous matter, missing evidence, tool outage, malicious embedded instructions, cross-matter request, malformed output, unsupported citation, budget exhaustion and prohibited-action attempt. Evaluate accuracy, provenance, abstention, intervention and cost. Zero observed unauthorized actions or leaks is a release requirement, not guarantee. No live data in evaluation sets.

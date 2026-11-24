---
name: wl-04-source-extraction-retrieval
description: >
  Manages minimal-copy source_reference provenance, Dropbox/Gmail version pinning, normalized top-left bounding boxes, Bates/line spans, and pre-filtered PostgreSQL retrieval. Use when parsing documents, storing provenance coordinates, or building scoped full-text and vector search. Do not duplicate original Dropbox repositories without explicit authorization.
---

# WL-04: Source-Referenced Extraction, Coordinates & Scoped Postgres Retrieval

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Provenance & Scoped Retrieval Rules

1. **Source Reference & Minimal-Copy Discipline:**
   - Respect Dropbox as original document authority. Store `source_reference` rows with `source_system`, `external_id`, `source_version` (e.g., Dropbox `rev`/content hash or Gmail `historyId`/`messageId`), `sha256`, and inherited access scope.
   - Store normalized top-left page bounding boxes (`page_number`, `[x0, y0, x1, y1]` in `0.0..1.0`) and Bates/line spans on extracted segments so every finding links to verifiable coordinates.

2. **Dual Text Representation:**
   - Keep `raw_excerpt` unmodified for exact quote verification, alongside `scoped_search_text` with deterministic provenance headers for PostgreSQL full-text (`tsvector`/BM25) and optional `pgvector` retrieval.
   - Enforce access-scope filtering BEFORE retrieval scoring.

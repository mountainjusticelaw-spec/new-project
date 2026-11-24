---
name: wl-08-docx-ooxml-redline-compiler
description: >
  Compiles reversible Microsoft Word (.docx) OOXML Tracked Changes (<w:ins>, <w:del>) and negotiation-safe margin comments from structured JSON diffs without exposing internal strategy. Use when parsing .docx files, applying contract or pleading redlines, or exporting Word work products. Do not feed raw word/document.xml to Gemini ADK models or emit raw XML from LLMs.
---

# WL-08: Reversible .docx OOXML Redline & Work-Product Compiler

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Reversible OOXML Pipeline

1. Never feed raw `word/document.xml` to Gemini ADK agents or let models emit raw OOXML.
2. Flatten `.docx` drafts and agreements into indexed paragraphs (`[P-001]...`) with run-offset maps, have ADK specialists emit structured JSON diffs plus rationale, and compile deterministic `<w:ins>`, `<w:del>`, and negotiation-safe `comments.xml` (`lxml`/`python-docx`) while keeping privileged internal attorney strategy inside `work_product` metadata only.

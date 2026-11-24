# Incoming correspondence review — proposed v0.1

Goal: one synthetic email and attached synthetic court document produce a source-linked review packet for the correct synthetic matter without external effects.

1. Mock ingress validates event envelope and dedup key.
2. Resolve exact synthetic matter; ambiguous or unmatched -> QUARANTINED.
3. Extract attachment with source version/page references and extraction-gap flags.
4. Evidence agent inventories; docket agent flags potential dates (not verified deadlines); practice agent summarizes claims and possible next steps.
5. Senior reviewer checks source support, uncertainty, adverse issues and missing records.
6. Store draft packet and proposals with policy/version metadata; mark REVIEW_REQUIRED. No executor writes.
7. Simulate approve/reject/expire paths locally; approved mock action is logged only; no actual provider call.

Future Gmail adapter: authorized mailbox watch -> Pub/Sub notification -> history.list reconciliation -> scoped message fetch. Persist checkpoint only after durable event acceptance; renew watch daily, handle expiration/history gaps, deduplicate messages and reconcile periodically. Source: [Gmail push guide](https://developers.google.com/workspace/gmail/api/guides/push). Never enable a watch in this baseline.

Acceptance: normal packet with source pointers; duplicate no extra job; ambiguous matter quarantined; missing attachment visibly incomplete; injected instructions ignored; failed tool causes bounded retry/exception; stale or denied approval never writes. Human validates outcome using synthetic example.

---
name: wl-02-georgia-law-grpc
description: >
  Applies Georgia procedural law (O.C.G.A. Title 9 Ch. 11, USCR, Title 24 Evidence), statutory deadline triggers, SB 68/SB 69 civil tort reforms, and GRPC/UPL supervision controls. Use when triaging correspondence, computing legal deadlines, drafting prompts, or labeling work products. Do not use Federal Rules (FRCP/FRE) where Georgia law governs.
---

# WL-02: Georgia Law, Deadlines & GRPC Compliance

> **Governance Notice:** Operates strictly under WeaverLawAgents `AGENTS.md`, `ARCHITECTURE.md`, and `SECURITY.md`. Managing-attorney authorization and repository governance always take precedence.

## Georgia Practice & Ethical Controls

1. **Incoming Correspondence Triage Rules (Georgia Practice):**
   - Flag statutory and procedural triggers immediately from synthetic correspondence, court notices, and demands:
     - **Municipal Ante Litem:** 6 months (`O.C.G.A. § 36-33-5`); **County/State Ante Litem:** 12 months (`O.C.G.A. § 36-11-1` / `§ 50-21-26`).
     - **Civil Answer:** 30 days (`O.C.G.A. § 9-11-12(a)`); **Default Opening as of right:** 15 days (`O.C.G.A. § 9-11-55(a)`).
     - **Motor Vehicle Time-Limited Demands:** `O.C.G.A. § 9-11-67.1` (30-day minimum window + statutory terms).
     - **Contractual Attorney's Fees Notice:** 10 days (`O.C.G.A. § 13-1-11`).
     - **Direct Appeals:** Notice of Appeal due within 30 days after entry of an appealable decision or judgment under `O.C.G.A. § 5-6-38(a)`, for judgments appealable under `O.C.G.A. § 5-6-34(a)`, subject to shorter specific statutory clocks such as 7 days in dispossessory actions under `O.C.G.A. § 44-7-56`.
     - **Interlocutory Appeals (`O.C.G.A. § 5-6-34(b)`):** Certificate of Immediate Review within 10 days of order entry; application within 10 days of certificate entry; AND Notice of Appeal within 10 days after entry of the order granting the application.
     - **Discretionary Appeals (`O.C.G.A. § 5-6-35(d), (g)`):** Application within 30 days of order entry unless a shorter specific statute applies; AND Notice of Appeal within 10 days after entry of the order granting the application (`O.C.G.A. § 5-6-35(g)`).
   - **Apply SB 68 / SB 69 Civil Rules where relevant:** `O.C.G.A. § 9-11-12(j)` (90-day discovery stay on motion to dismiss), `O.C.G.A. § 9-11-41(a)` (60-day post-answer voluntary dismissal limit), `O.C.G.A. § 51-12-1.1` (billed vs. paid medical damages and LOP discoverability for post-April 21, 2025 injuries), `O.C.G.A. § 40-8-76.1`, `O.C.G.A. § 51-3-50`, and `O.C.G.A. § 51-12-15`.
   - Never substitute Federal Rules (FRCP/FRE) where Georgia's Civil Practice Act (`Title 9, Ch. 11`), `USCR`, or `Title 24` Evidence Code govern.

2. **Ethical & UPL Controls (`GRPC 1.6, 3.3, 5.3` & `O.C.G.A. § 15-19-51`):**
   - Stamp all generated `work_product` records as `PROPOSED WORK PRODUCT - REQUIRES GEORGIA ATTORNEY REVIEW`.
   - Agents propose deadline/calendar entries as `action_proposal` rows only; agents never mutate official dockets or clear conflicts autonomously.

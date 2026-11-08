# Gusan iOS — planning package (hand-off for any coding agent)
Read in this order:
1. `BRIEF.md` — owner's verbatim requirements and corrections (source of truth).
2. `design/v3/Gusan-iPhone-Design-v3.html` — current design (23 frames) + PNG exports. `design/v3/critique/` — HIG review (33 findings; 3 blockers) with annotated screenshots. `design/drafts/` — superseded mockups.
3. `spec/Gusan-iOS-technical-specification-v2.md` — current technical spec (v1 + security/engineering review folded in; §12 tickets, §13 spikes). `spec/Gusan-iOS-security-and-engineering-review.md` — the review itself.
4. `research/` — H Company docs (vaults, browser profiles, sessions).
5. `hackathon/` — product description and demo plan.
Build branch: `devin/1794100378-ios-hackathon` (iOS keyboard + agent tasks + Gusan Screen + agent-action backend merged). CI: `.github/workflows/ios.yml` produces an unsigned IPA artifact.

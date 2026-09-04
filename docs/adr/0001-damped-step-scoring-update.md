---
status: accepted
---

# Damped-step Score updates, direct-set Positionnement, unified mastery thresholds

## Context

The additive bonus/malus table in `update_scores` applied a fixed delta per answer regardless of how much history a Compétence already had, so a Score could swing ±0.4 forever — `attempts_count` was stored but never consulted. A separate bug in the retry UI let a wrong-then-right answer charge the malus *and* the full bonus on the same question. Positionnement never called `/session/submit` at all, so the placement test produced a session-local percentage but left every Compétence at its 0.5 default — training always started blind. Finally, "mastered" was defined three different ways across the codebase (`Progression.level` thresholds, `choisir_competence`'s 0.8/70%, `deduire_niveau_eleve`'s 0.7).

## Decision

- **Score update is now a damped step**: `effective_delta = base_delta(type, niveau) * retry_factor(attempt) / (1 + attempts_count / 5)`, applied once per question. `base_delta` is the existing bonus/malus table, unchanged. `retry_factor` is 1.0 on the first attempt, 0.5 on the second, 0 on the third (a correct answer after the full explanation on attempt 3 no longer signals unaided mastery). The malus fires at most once per question, at the first wrong attempt — it never stacks with a later retry bonus.
- **Positionnement and Évaluation direct-set the Score** for each tested Compétence at session end (correct → 0.65, wrong → 0.35) instead of running through the update rule above, and mark `attempts_count` so `is_first_session` correctly flips to false afterward. A placement test is a calibration snapshot, not a training event; one question shouldn't move a Score by the same ±0.4 a repeated training answer would.
- **One canonical mastery definition**, used everywhere it's checked: Level bucket `faible` (<0.4) / `moyen` (<0.75) / `avance` (≥0.75) from Score alone; **Mastered** additionally requires `attempts_count ≥ 3`.

Out of scope, left as-is: `choisir_competence`'s random (not weakest-first) selection among candidates, and `deduire_niveau_eleve`'s 0.7 tier-promotion threshold. Both still disagree with the thresholds above — known, deliberate debt, not an oversight.

## Considered options

- **Retune the flat table instead of damping.** Rejected: fixes nothing about accumulation — a Score would still swing by a fixed amount on the 50th answer as on the 1st.
- **Feed Positionnement through the same update rule from 0.5.** Rejected: with one question per Compétence, a single answer's fixed delta is a noisy placement, not a calibration.
- **Scale QCM bonus by the live option count (1/n guess odds).** Rejected: the table's existing QCM < QRO < SBS ordering already discounts for guessing; scaling by `n` adds a data dependency (threading option-count from generator to scoring) for marginal accuracy.
- **Rebalance the bonus/malus asymmetry (e.g. QCM facile +0.2/−0.4) while adding damping.** Rejected: the asymmetry encodes a separate intent (failing an easy question is worse news than acing a hard one) unrelated to the accumulation problem damping solves. Not two knobs in one pass.

## Consequences

- `attempts_count` becomes scoring-relevant — it was previously decorative.
- Existing preprod `Progression` rows are not migrated; they're disposable and get reset/recomputed on deploy.
- The Positionnement recap UI will now reflect real persisted Scores instead of the untouched 0.5 default.

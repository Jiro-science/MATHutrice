# MATHutrice

Adaptive math training app: students work through math topics, the app tracks per-skill mastery scores in a database, and picks each next question based on those scores.

## Language

**Notion**:
A math topic (e.g. "Trigonométrie") that groups a fixed set of Compétences. Defined statically in `REFERENTIEL`.
_Avoid_: Module, topic (in code/UI these appear but should map back to Notion).

**Compétence**:
A single atomic skill within a Notion, identified by a short code (e.g. `tr04`). Has a fixed Niveau tier and a live Score.
_Avoid_: Skill, competency.

**Niveau (of a Compétence)**:
The fixed difficulty tier a Compétence belongs to: `basique` | `solide` | `expert`. Assigned once in `REFERENTIEL` and never changes.
_Avoid_: Conflating with Niveau élève or Niveau question — all three are called "niveau" in code but mean different things.

**Niveau élève**:
The tier (`basique` | `solide` | `expert`) a student is currently working at *for a given Notion*, deduced from the average Score of that student's Compétences at each tier (`deduire_niveau_eleve`). Governs which Compétences get selected for the next training question.
_Avoid_: Niveau alone when this is meant.

**Niveau question**:
The difficulty label attached to a generated question for scoring purposes: `facile` | `intermediaire` | `difficile`. Derived 1:1 from a Compétence's Niveau (`basique`→`facile`, `solide`→`intermediaire`, `expert`→`difficile`) purely to key the bonus/malus lookup table.
_Avoid_: Niveau alone when this is meant; don't introduce a fourth vocabulary for the same idea.

**Score**:
A Compétence's mastery value in `[0, 1]` for one student, stored in `Progression.score`. Adjusted by a bonus/malus after each answer and clamped to `[0, 1]`. The size of that adjustment shrinks as the Compétence accumulates attempts, so a Score with a track record resists being swung by a single answer the way a fresh one can.

**Compétence maîtrisée** (Mastered):
A Compétence is Mastered for a student when Score ≥ 0.8 **and** it has been attempted at least 3 times. Stronger than the `avance` Level bucket alone — the attempt-count bar exists so a single lucky answer can't flip a Compétence to "mastered."

**REFERENTIEL**:
The static, in-memory catalogue of every Notion and its Compétences (names, codes, Niveau tiers). Its own per-competence `score` field is a placeholder default — a student's real Score always comes from `Progression` and overwrites it before use.

**Progression**:
The DB record of one student's Score, Level, and attempt count for one Compétence.

**Level**:
A display bucket derived from a Progression's Score for UI purposes: `faible` (Score < 0.4), `moyen` (0.4 ≤ Score < 0.75), `avance` (Score ≥ 0.75). Distinct from Niveau (basique/solide/expert) — different axis, different values.
_Avoid_: Niveau, when Level (the UI bucket) is meant.

**Session**:
One run through questions for a Notion. Three kinds:
- **Positionnement** — first-time placement test sampling all three Niveaux at once. Sets each tested Compétence's Score directly from correctness (a calibration snapshot), rather than through the incremental bonus/malus adjustment Entraînement uses.
- **Entraînement** — ongoing training, one adaptive question at a time, Niveau chosen via Niveau élève.
- **Évaluation** — a longer, configurable-size summative test.

**Streak**:
Count of consecutive correct answers within a training Session. UI-only, not persisted.

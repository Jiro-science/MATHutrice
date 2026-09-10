# MATHutrice

Tuteur de mathématiques piloté par LLM : génère des exercices adaptés au niveau de l'élève et diagnostique ses lacunes.

## Language

**REFERENTIEL**:
Dict Python en mémoire (`fonctions_python/main.py`) donnant le catalogue statique des notions et compétences (code, nom, niveau, score par défaut) utilisé pour générer des exercices et diagnostiquer des réponses.
_Avoid_: "le référentiel" seul (ambigu avec les tables `Notion`/`Competence` en base), "config".

**Notion**:
Table DB (et clé de premier niveau dans REFERENTIEL) représentant un grand thème mathématique (ex. "Trigonométrie"). Porte un `title` et une `description` affichés côté web.

**Competence**:
Table DB (et entrée feuille dans REFERENTIEL) représentant une compétence évaluable au sein d'une Notion. Identifiée par un code stable (`referentiel_code`), rattachée à un `niveau`/`level` (`basique` | `solide` | `expert`). Porte un `title` (court, affiché à l'élève) et une `description` (long, utilisé pour construire les prompts de génération), au même titre que `Notion`.

**Progression**:
Table DB stockant le score courant d'un élève pour une Competence donnée — c'est la source de vérité pour la maîtrise réelle d'un élève connecté, toujours interrogée par `(sso_id, competence_id)`.

**referentiel_key / referentiel_code**:
Clé texte stable qui relie une entrée REFERENTIEL à sa ligne DB (`Notion.referentiel_key`, `Competence.referentiel_code`) — c'est la clé de jointure entre les deux sources.

**Divergence point** (transitoire, voir [[0002-referentiel-db-divergence-policy]]):
Endroit du code qui tolère déjà que REFERENTIEL et la DB soient en désaccord (une entrée existe d'un côté sans équivalent de l'autre) en appliquant une valeur par défaut ou en sautant l'opération silencieusement, plutôt que d'échouer. 4 points identifiés à date.

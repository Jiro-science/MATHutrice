"""
session_generator.py — Génère une session d'entraînement mixte (QCM / QRO / SBS)
à partir des scores réels de l'élève en base de données.

Flow :
  1. Vérifier si c'est la première fois (aucune progression en DB pour cette notion)
  2. Si première fois → positionnement : 5 QCM + 3 QRO + 2 SBS
  3. Sinon → entraînement : une question à la fois via generate_exercise_randomly
  4. Les scores sont persistés en DB après chaque réponse via /submit_answer
"""

import logging
import random
from decimal import Decimal
from datetime import datetime, UTC
import uuid
from uuid import UUID
from sqlmodel import Session, select

import models
import sys, os

sys.path.insert(0, os.path.dirname(__file__))
from main import (
    generate_mixed_test,
    generate_exercise_randomly,
)
import notion_catalogue

logger = logging.getLogger(__name__)




def get_competence_map_by_codes(codes: list[str], db: Session) -> dict[str, models.Competence]:
    """
    Convertit les codes du REFERENTIEL vers les lignes Competence en BDD.

    Exemple :
    tr01 -> Competence(competence_id=UUID(...), referentiel_code="tr01")
    """
    if not codes:
        return {}

    rows = db.exec(
        select(models.Competence).where(
            models.Competence.referentiel_code.in_(codes)
        )
    ).all()

    return {row.referentiel_code: row for row in rows}


def get_notion_by_referentiel_key(referentiel_key: str, db: Session) -> models.Notion | None:
    """
    Récupère la notion BDD à partir de la clé du REFERENTIEL.
    Exemple : trigonometrie -> notion_id UUID
    """
    return db.exec(
        select(models.Notion).where(
            models.Notion.referentiel_key == referentiel_key
        )
    ).first()


def _get_notion_or_raise(notion_key: str, db: Session) -> notion_catalogue.NotionCatalogue:
    """Lève ValueError (au lieu de UnknownNotionKeyError) si la clé est inconnue en BDD."""
    try:
        return notion_catalogue.get_notion(notion_key, db)
    except notion_catalogue.UnknownNotionKeyError as e:
        raise ValueError(str(e)) from e




# ─── NIVEAU AUTO ──────────────────────────────────────────────────────────────


def deduire_niveau_eleve(notion_data: dict) -> str:
    """
    Déduit le niveau de l'élève pour une notion donnée d'après les scores.

    Règle :
      - Moyenne basique < 0.7           → basique
      - Moyenne basique >= 0.7
        ET moyenne solide < 0.7         → solide
      - Moyenne basique >= 0.7
        ET moyenne solide >= 0.7        → expert
    """

    def moyenne_niveau(niveau: str) -> float:
        comps = [c for c in notion_data["competences"] if c["niveau"] == niveau]
        if not comps:
            return 1.0  # niveau inexistant → considéré acquis
        return sum(c["score"] for c in comps) / len(comps)

    if moyenne_niveau("basique") < 0.7:
        return "basique"
    if moyenne_niveau("solide") < 0.7:
        return "solide"
    return "expert"


# ─── INJECTION SCORES DB ──────────────────────────────────────────────────────


def build_notion_data_with_scores(
    notion_key: str,
    sso_id: UUID,
    db: Session,
) -> dict:
    """
    Construit la structure de la notion (table Notion/Competence en base) avec
    les vrais scores de l'élève injectés depuis la table progression.

    Chaque compétence porte `nom` (la description longue, destinée aux prompts
    de génération d'exercices) et `title` (le libellé court, destiné à un
    affichage éventuel côté appelant) comme deux champs distincts.

    Politique de repli transitoire (docs/adr/0002-referentiel-db-divergence-policy.md) :
    un score de repli (0.5) est utilisé aussi bien pour une compétence jamais
    tentée par l'élève (aucune ligne progression) que pour une compétence
    absente de la BDD malgré sa présence dans le catalogue de la notion — les
    deux cas sont journalisés séparément pour rester distinguables.
    """
    notion = _get_notion_or_raise(notion_key, db)

    codes = [c.code for c in notion.competences]
    code_to_competence = get_competence_map_by_codes(codes, db)
    competence_ids = [comp.competence_id for comp in code_to_competence.values()]

    rows = (
        db.exec(
            select(models.Progression).where(
                models.Progression.sso_id == sso_id,
                models.Progression.competence_id.in_(competence_ids),
            )
        ).all()
        if competence_ids
        else []
    )

    id_to_code = {
        comp.competence_id: code
        for code, comp in code_to_competence.items()
    }

    attempted_codes = set()
    scores_db = {}
    for row in rows:
        if row.competence_id not in id_to_code:
            continue
        code = id_to_code[row.competence_id]
        attempted_codes.add(code)
        scores_db[code] = float(row.score)

    competences = []

    for entry in notion.competences:
        if entry.code not in code_to_competence:
            logger.warning(
                "[SCORES] Compétence %s absente de la BDD malgré sa présence "
                "dans le catalogue de la notion %s — score de repli utilisé.",
                entry.code,
                notion_key,
            )
        elif entry.code not in attempted_codes:
            logger.info(
                "[SCORES] Compétence %s jamais tentée par l'élève %s — "
                "score de repli utilisé.",
                entry.code,
                sso_id,
            )

        competences.append(
            {
                "code": entry.code,
                "nom": entry.description,
                "title": entry.title,
                "niveau": entry.level,
                "score": scores_db.get(entry.code, 0.5),
            }
        )

    return {
        "notion_nom": notion.description,
        "notion_title": notion.title,
        "competences": competences,
    }


# ─── VÉRIFICATION PREMIÈRE FOIS ───────────────────────────────────────────────


def is_first_session(notion_key: str, sso_id: UUID, db: Session) -> bool:
    notion = _get_notion_or_raise(notion_key, db)

    codes = [c.code for c in notion.competences]

    code_to_competence = get_competence_map_by_codes(codes, db)
    competence_ids = [comp.competence_id for comp in code_to_competence.values()]

    if not competence_ids:
        return True

    attempted = db.exec(
        select(models.Progression).where(
            models.Progression.sso_id == sso_id,
            models.Progression.competence_id.in_(competence_ids),
            models.Progression.attempts_count > 0,
        )
    ).first()

    return attempted is None


# ─── INIT PROGRESSION ─────────────────────────────────────────────────────────


def init_progressions_for_user(sso_id: UUID, db: Session) -> None:
    """
    Initialise les entrées de progression pour toutes les compétences du
    catalogue (Notion/Competence en base) pour un nouvel élève.

    Score initial = 0.50.
    level initial = moyen.
    updated_at = None car la compétence n'a pas encore été travaillée.

    Lève ValueError si une compétence du catalogue n'a pas de ligne
    correspondante en BDD — politique stricte en écriture (voir
    docs/adr/0002-referentiel-db-divergence-policy.md) : un score n'est jamais
    silencieusement abandonné.
    """
    catalogue = notion_catalogue.get_competence_catalogue(db)
    codes = [entry.code for entry in catalogue]
    code_to_competence = get_competence_map_by_codes(codes, db)

    for entry in catalogue:
        competence = code_to_competence.get(entry.code)

        if not competence:
            raise ValueError(
                f"Compétence introuvable en BDD malgré sa présence dans le "
                f"catalogue : {entry.code}"
            )

        existing = db.exec(
            select(models.Progression).where(
                models.Progression.sso_id == sso_id,
                models.Progression.competence_id == competence.competence_id,
            )
        ).first()

        if existing:
            continue

        prog = models.Progression(
            progression_id=uuid.uuid4(),
            score=Decimal("0.50"),
            updated_at=None,
            level="moyen",
            attempts_count=0,
            competence_id=competence.competence_id,
            sso_id=sso_id,
        )
        db.add(prog)

    db.commit()


# ─── PERSISTANCE SCORE APRÈS RÉPONSE ─────────────────────────────────────────


def persist_score_update(
    sso_id: UUID,
    competences_dict: dict,  # { "tr01": True, "tr04": False }
    question_type: str,      # "QCM" | "QRO" | "SBS"
    question_niveau: str,    # "basique" | "solide" | "expert"
    db: Session,
) -> dict:
    """
    Applique les règles de scoring de update_scores() et persiste
    les nouveaux scores en base de données.

    Convertit toujours referentiel_code -> competence_id UUID via la BDD.

    Lève ValueError si un code de compétence n'a pas de ligne correspondante
    en BDD — politique stricte en écriture (voir
    docs/adr/0002-referentiel-db-divergence-policy.md) : un score n'est jamais
    silencieusement abandonné.
    """
    from fonctions_python.base_generator import update_scores

    # 1. Codes touchés par la question : ["tr01", "tr04", ...]
    codes = list(competences_dict.keys())

    # 2. Convertir les codes en vraies compétences BDD
    code_to_competence = get_competence_map_by_codes(codes, db)

    missing_codes = [code for code in codes if code not in code_to_competence]
    if missing_codes:
        raise ValueError(f"Compétence(s) introuvable(s) en BDD : {missing_codes}")

    competence_ids = [
        competence.competence_id
        for competence in code_to_competence.values()
    ]

    # 3. Récupérer les progressions existantes avec les UUID
    rows = db.exec(
        select(models.Progression).where(
            models.Progression.sso_id == sso_id,
            models.Progression.competence_id.in_(competence_ids),
        )
    ).all()

    # 4. Convertir competence_id UUID -> code référentiel
    id_to_code = {
        competence.competence_id: code
        for code, competence in code_to_competence.items()
    }

    scores_db = {
        id_to_code[row.competence_id]: float(row.score)
        for row in rows
        if row.competence_id in id_to_code
    }

    # 5. Référentiel minimal (une seule "notion" fictive) pour appeler
    # update_scores(), qui n'a besoin que de code + score par compétence touchée.
    local_ref = {
        "_notion_fictive": {
            "competences": [
                {"code": code, "score": scores_db.get(code, 0.5)}
                for code in codes
            ]
        }
    }

    # 6. Adapter le niveau pour update_scores()
    niveau_map = {
        "basique": "facile",
        "solide": "intermediaire",
        "expert": "difficile",
    }

    q_niveau = niveau_map.get(question_niveau, "intermediaire")

    question_format = {
        "type": question_type.upper(),
        "niveau": q_niveau,
    }

    # 7. Calculer les nouveaux scores
    _, _, nouveaux_scores = update_scores(
        local_ref,
        question_format,
        competences_dict,
    )

    # 8. Sauvegarder en BDD
    now = datetime.now(UTC).replace(tzinfo=None)

    for code, new_score in nouveaux_scores.items():
        competence = code_to_competence[code]

        clamped = max(0.0, min(1.0, new_score))

        if clamped < 0.4:
            level = "faible"
        elif clamped < 0.75:
            level = "moyen"
        else:
            level = "avance"

        prog = db.exec(
            select(models.Progression).where(
                models.Progression.sso_id == sso_id,
                models.Progression.competence_id == competence.competence_id,
            )
        ).first()

        if prog:
            prog.score = Decimal(str(round(clamped, 2)))
            prog.level = level
            prog.updated_at = now
            prog.attempts_count += 1
            db.add(prog)
        else:
            # Sécurité : créer si absent
            prog = models.Progression(
                progression_id=uuid.uuid4(),
                score=Decimal(str(round(clamped, 2))),
                updated_at=now,
                level=level,
                attempts_count=1,
                competence_id=competence.competence_id,
                sso_id=sso_id,
            )
            db.add(prog)

    db.commit()

    return {
        code: max(0.0, min(1.0, score))
        for code, score in nouveaux_scores.items()
    }


# ─── GÉNÉRATION POSITIONNEMENT ────────────────────────────────────────────────


def generate_positioning_session(notion_key: str, sso_id: UUID, db: Session) -> dict:
    """
    Génère le test de positionnement sur les 3 niveaux.
    Distribution : 1 basique (3 questions) + 1 solide (6 questions) + 1 expert (2 questions)
    Adapté au nombre de compétences par niveau dans la notion.
    """
    notion_data = build_notion_data_with_scores(notion_key, sso_id, db)

    q_basique = generate_mixed_test(
        notion=notion_key,
        niveau="basique",
        n_qcm=1,
        n_qro=1,
        n_steps=1,
        notion_data_override=notion_data,
    )
    q_solide = generate_mixed_test(
        notion=notion_key,
        niveau="solide",
        n_qcm=3,
        n_qro=2,
        n_steps=1,
        notion_data_override=notion_data,
    )
    q_expert = generate_mixed_test(
        notion=notion_key,
        niveau="expert",
        n_qcm=1,
        n_qro=1,
        n_steps=0,
        notion_data_override=notion_data,
    )

    questions = q_basique + q_solide + q_expert

    # Dédupliquer par compétence
    seen = set()
    unique_questions = []
    for q in questions:
        comp_code = (q.get("competence_cible") or {}).get("code", "")
        key = comp_code + q.get("type", "")
        if key not in seen:
            seen.add(key)
            unique_questions.append(q)
    questions = unique_questions
    random.shuffle(questions)

    # Ici "notion_nom" est le nom du champ de sortie attendu par l'appelant
    # (affichage) : on y met le titre court (notion_data["notion_title"]), pas
    # notion_data["notion_nom"] (la description longue utilisée ci-dessus pour
    # les prompts de génération via notion_data_override).
    return {
        "session_type": "positionnement",
        "notion_nom": notion_data["notion_title"],
        "notion_key": notion_key,
        "niveau_eleve": "mixte",
        "questions": questions,
    }


# ─── GÉNÉRATION QUESTION ENTRAÎNEMENT ─────────────────────────────────────────


def generate_next_question(notion_key: str, sso_id: UUID, db: Session) -> dict:
    """
    Génère une seule question pour la phase d'entraînement.
    Le type (qcm/qro/sbs) est choisi aléatoirement.
    Le niveau est déduit des scores actuels de l'élève.
    """
    notion_data = build_notion_data_with_scores(notion_key, sso_id, db)
    niveau_eleve = deduire_niveau_eleve(notion_data)

    questions = generate_exercise_randomly(
        {notion_key: notion_data}, niveau_eleve, notion_key
    )

    # Voir generate_positioning_session : "notion_nom" en sortie = titre court,
    # distinct de notion_data["notion_nom"] (description longue) utilisé plus
    # haut pour la génération.
    return {
        "session_type": "entrainement",
        "notion_nom": notion_data["notion_title"],
        "notion_key": notion_key,
        "niveau_eleve": niveau_eleve,
        "questions": questions,
    }

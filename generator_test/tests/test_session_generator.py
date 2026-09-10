from uuid import uuid4

import pytest
from sqlmodel import Session, select

import models
from fonctions_python.session_generator import (
    build_notion_data_with_scores,
    deduire_niveau_eleve,
    init_progressions_for_user,
    is_first_session,
    persist_score_update,
)


def make_notion_data(*, basique=None, solide=None, expert=None) -> dict:
    """Construit un dict notion minimal avec les scores donnés par niveau."""
    competences = []
    for niveau, scores in (("basique", basique), ("solide", solide), ("expert", expert)):
        for score in scores or []:
            competences.append({"niveau": niveau, "score": score})
    return {"competences": competences}


def test_deduire_niveau_eleve_returns_basique_when_basique_average_below_threshold():
    notion_data = make_notion_data(basique=[0.5, 0.6], solide=[0.9], expert=[0.9])

    assert deduire_niveau_eleve(notion_data) == "basique"


def test_deduire_niveau_eleve_returns_solide_when_basique_ok_but_solide_below_threshold():
    notion_data = make_notion_data(basique=[0.8, 0.9], solide=[0.4], expert=[0.9])

    assert deduire_niveau_eleve(notion_data) == "solide"


def test_deduire_niveau_eleve_returns_expert_when_basique_and_solide_ok():
    notion_data = make_notion_data(basique=[0.8], solide=[0.75], expert=[0.1])

    assert deduire_niveau_eleve(notion_data) == "expert"


def test_deduire_niveau_eleve_treats_missing_niveau_as_acquired():
    # Aucune compétence "basique" dans les données -> moyenne considérée à 1.0,
    # donc on ne peut pas retomber sur "basique" faute de données.
    notion_data = make_notion_data(solide=[0.5], expert=[0.9])

    assert deduire_niveau_eleve(notion_data) == "solide"


def test_deduire_niveau_eleve_threshold_is_inclusive():
    # Une moyenne exactement à 0.7 n'est pas < 0.7, donc considérée acquise.
    notion_data = make_notion_data(basique=[0.7], solide=[0.7], expert=[0.0])

    assert deduire_niveau_eleve(notion_data) == "expert"


# ─── Helpers BDD ───────────────────────────────────────────────────────────────


def make_notion(db: Session, key: str, title: str, description: str) -> models.Notion:
    notion = models.Notion(
        notion_id=uuid4(),
        referentiel_key=key,
        title=title,
        description=description,
    )
    db.add(notion)
    db.commit()
    db.refresh(notion)
    return notion


def make_competence(
    db: Session,
    notion: models.Notion,
    code: str,
    title: str,
    description: str,
    level: str = "basique",
) -> models.Competence:
    competence = models.Competence(
        competence_id=uuid4(),
        referentiel_code=code,
        title=title,
        description=description,
        level=level,
        notion_id=notion.notion_id,
    )
    db.add(competence)
    db.commit()
    db.refresh(competence)
    return competence


def make_progression(
    db: Session,
    sso_id,
    competence: models.Competence,
    score: str,
    attempts_count: int = 1,
) -> models.Progression:
    from decimal import Decimal

    prog = models.Progression(
        progression_id=uuid4(),
        score=Decimal(score),
        updated_at=None,
        level="moyen",
        attempts_count=attempts_count,
        competence_id=competence.competence_id,
        sso_id=sso_id,
    )
    db.add(prog)
    db.commit()
    return prog


# ─── is_first_session ──────────────────────────────────────────────────────────


def test_is_first_session_raises_value_error_for_unknown_notion_key(db: Session):
    with pytest.raises(ValueError):
        is_first_session("clef_inexistante", uuid4(), db)


def test_is_first_session_true_when_no_attempts_yet(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigonométrie", "desc trigo")
    make_competence(db, notion, "tr01", "Titre court", "Description longue")

    assert is_first_session("trigonometrie", uuid4(), db) is True


def test_is_first_session_false_once_an_attempt_was_made(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigonométrie", "desc trigo")
    competence = make_competence(db, notion, "tr01", "Titre court", "Description longue")
    sso_id = uuid4()
    make_progression(db, sso_id, competence, score="0.70", attempts_count=1)

    assert is_first_session("trigonometrie", sso_id, db) is False


# ─── build_notion_data_with_scores ─────────────────────────────────────────────


def test_build_notion_data_with_scores_raises_value_error_for_unknown_notion_key(db: Session):
    with pytest.raises(ValueError):
        build_notion_data_with_scores("clef_inexistante", uuid4(), db)


def test_build_notion_data_with_scores_exposes_title_and_description_as_distinct_fields(
    db: Session,
):
    notion = make_notion(db, "trigonometrie", "Trigo", "Description longue de la notion")
    make_competence(
        db,
        notion,
        "tr01",
        title="Titre court",
        description="Description longue de la compétence",
        level="basique",
    )

    result = build_notion_data_with_scores("trigonometrie", uuid4(), db)

    assert result["notion_title"] == "Trigo"
    assert result["notion_nom"] == "Description longue de la notion"

    comp = result["competences"][0]
    assert comp["title"] == "Titre court"
    assert comp["nom"] == "Description longue de la compétence"
    assert comp["niveau"] == "basique"


def test_build_notion_data_with_scores_falls_back_when_never_attempted(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    make_competence(db, notion, "tr01", "Titre", "Description")

    result = build_notion_data_with_scores("trigonometrie", uuid4(), db)

    assert result["competences"][0]["score"] == 0.5


def test_build_notion_data_with_scores_uses_real_progression_score(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    competence = make_competence(db, notion, "tr01", "Titre", "Description")
    sso_id = uuid4()
    make_progression(db, sso_id, competence, score="0.85")

    result = build_notion_data_with_scores("trigonometrie", sso_id, db)

    assert result["competences"][0]["score"] == pytest.approx(0.85)


# ─── init_progressions_for_user ────────────────────────────────────────────────


def test_init_progressions_for_user_creates_a_row_per_competence(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    make_competence(db, notion, "tr01", "Titre 1", "Desc 1")
    make_competence(db, notion, "tr02", "Titre 2", "Desc 2")
    sso_id = uuid4()

    init_progressions_for_user(sso_id, db)

    rows = db.exec(
        select(models.Progression).where(models.Progression.sso_id == sso_id)
    ).all()
    assert len(rows) == 2
    assert all(row.attempts_count == 0 for row in rows)
    assert all(str(row.score) == "0.50" for row in rows)


def test_init_progressions_for_user_does_not_duplicate_existing_rows(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    competence = make_competence(db, notion, "tr01", "Titre", "Desc")
    sso_id = uuid4()
    make_progression(db, sso_id, competence, score="0.90", attempts_count=3)

    init_progressions_for_user(sso_id, db)

    rows = db.exec(
        select(models.Progression).where(models.Progression.sso_id == sso_id)
    ).all()
    assert len(rows) == 1
    assert rows[0].attempts_count == 3
    assert str(rows[0].score) == "0.90"


# ─── persist_score_update ──────────────────────────────────────────────────────


def test_persist_score_update_raises_value_error_for_unknown_competence_code(db: Session):
    with pytest.raises(ValueError):
        persist_score_update(
            sso_id=uuid4(),
            competences_dict={"code_inexistant": True},
            question_type="qcm",
            question_niveau="basique",
            db=db,
        )


def test_persist_score_update_creates_progression_row_when_none_existed(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    competence = make_competence(db, notion, "tr01", "Titre", "Desc")
    sso_id = uuid4()

    result = persist_score_update(
        sso_id=sso_id,
        competences_dict={"tr01": True},
        question_type="qcm",
        question_niveau="basique",
        db=db,
    )

    # Score de départ 0.5 (jamais tenté) + bonus QCM/facile (0.2) = 0.7
    assert result["tr01"] == pytest.approx(0.7)

    prog = db.exec(
        select(models.Progression).where(
            models.Progression.sso_id == sso_id,
            models.Progression.competence_id == competence.competence_id,
        )
    ).first()
    assert prog is not None
    assert prog.attempts_count == 1
    assert float(prog.score) == pytest.approx(0.7)


def test_persist_score_update_updates_existing_progression_row(db: Session):
    notion = make_notion(db, "trigonometrie", "Trigo", "desc")
    competence = make_competence(db, notion, "tr01", "Titre", "Desc")
    sso_id = uuid4()
    make_progression(db, sso_id, competence, score="0.50", attempts_count=2)

    result = persist_score_update(
        sso_id=sso_id,
        competences_dict={"tr01": False},
        question_type="qcm",
        question_niveau="basique",
        db=db,
    )

    # 0.5 + malus QCM/facile (-0.4) = 0.1
    assert result["tr01"] == pytest.approx(0.1)

    prog = db.exec(
        select(models.Progression).where(
            models.Progression.sso_id == sso_id,
            models.Progression.competence_id == competence.competence_id,
        )
    ).first()
    assert prog.attempts_count == 3

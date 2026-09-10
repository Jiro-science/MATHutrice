from uuid import uuid4

import pytest
from sqlmodel import Session

import models
from fonctions_python.notion_catalogue import (
    UnknownNotionKeyError,
    get_competence_catalogue,
    get_notion,
)


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


def test_get_notion_returns_full_structure_with_competences(db: Session):
    trigo = make_notion(
        db,
        key="trigonometrie",
        title="Trigonométrie",
        description="Étude des fonctions trigonométriques, des angles et du cercle trigonométrique.",
    )
    make_competence(
        db,
        trigo,
        code="tr01",
        title="Convertir degrés et radians",
        description="Savoir convertir une mesure d'angle entre degrés et radians.",
        level="basique",
    )
    make_competence(
        db,
        trigo,
        code="tr02",
        title="Résoudre une équation trigonométrique",
        description="Résoudre des équations trigonométriques simples sur un intervalle donné.",
        level="expert",
    )

    result = get_notion("trigonometrie", db)

    assert result.key == "trigonometrie"
    assert result.title == "Trigonométrie"
    assert result.description == (
        "Étude des fonctions trigonométriques, des angles et du cercle trigonométrique."
    )
    assert {c.code for c in result.competences} == {"tr01", "tr02"}

    tr01 = next(c for c in result.competences if c.code == "tr01")
    assert tr01.title == "Convertir degrés et radians"
    assert tr01.description == (
        "Savoir convertir une mesure d'angle entre degrés et radians."
    )
    assert tr01.level == "basique"
    assert tr01.notion_title == "Trigonométrie"


def test_get_notion_title_and_description_are_distinct_fields(db: Session):
    notion = make_notion(db, key="fractions", title="Fractions", description="Long text about fractions.")
    make_competence(
        db,
        notion,
        code="fp01",
        title="Short title",
        description="Long prompt-facing description, different from the title.",
    )

    result = get_notion("fractions", db)
    competence = result.competences[0]

    assert competence.title != competence.description
    assert competence.title == "Short title"
    assert competence.description == "Long prompt-facing description, different from the title."


def test_get_notion_raises_for_unknown_key(db: Session):
    with pytest.raises(UnknownNotionKeyError):
        get_notion("clef_inexistante", db)


def test_get_competence_catalogue_returns_every_competence_across_notions(db: Session):
    trigo = make_notion(db, key="trigonometrie", title="Trigonométrie", description="desc trigo")
    fractions = make_notion(db, key="fractions", title="Fractions", description="desc fractions")

    make_competence(db, trigo, code="tr01", title="tr01 title", description="tr01 desc")
    make_competence(db, trigo, code="tr02", title="tr02 title", description="tr02 desc")
    make_competence(db, fractions, code="fp01", title="fp01 title", description="fp01 desc")

    catalogue = get_competence_catalogue(db)

    assert {c.code for c in catalogue} == {"tr01", "tr02", "fp01"}
    fp01 = next(c for c in catalogue if c.code == "fp01")
    assert fp01.title == "fp01 title"
    assert fp01.description == "fp01 desc"
    assert fp01.level == "basique"
    assert fp01.notion_title == "Fractions"

    tr01 = next(c for c in catalogue if c.code == "tr01")
    assert tr01.notion_title == "Trigonométrie"


def test_get_competence_catalogue_empty_db_returns_empty_list(db: Session):
    assert get_competence_catalogue(db) == []

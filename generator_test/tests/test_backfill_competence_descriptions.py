from uuid import uuid4

import pytest
from sqlmodel import Session

import models
from fonctions_python.backfill_competence_descriptions import (
    backfill_competence_descriptions,
    build_code_to_description,
)
from fonctions_python.main import REFERENTIEL


def test_build_code_to_description_covers_all_117_referentiel_competences():
    mapping = build_code_to_description()

    assert len(mapping) == 117
    assert mapping["tr01"] == next(
        c["nom"] for c in REFERENTIEL["trigonometrie"]["competences"] if c["code"] == "tr01"
    )


def test_backfill_populates_description_from_referentiel_nom(db: Session):
    notion = models.Notion(
        notion_id=uuid4(),
        referentiel_key="trigonometrie",
        title="Trigonométrie",
        description="placeholder",
    )
    db.add(notion)

    competence = models.Competence(
        competence_id=uuid4(),
        referentiel_code="tr01",
        title="placeholder title",
        description="",
        level="basique",
        notion_id=notion.notion_id,
    )
    db.add(competence)
    db.commit()

    expected_nom = next(
        c["nom"] for c in REFERENTIEL["trigonometrie"]["competences"] if c["code"] == "tr01"
    )

    updated = backfill_competence_descriptions(db)

    assert updated == 1
    db.refresh(competence)
    assert competence.description == expected_nom


def test_backfill_raises_for_competence_with_no_referentiel_entry(db: Session):
    notion = models.Notion(
        notion_id=uuid4(),
        referentiel_key="trigonometrie",
        title="Trigonométrie",
        description="placeholder",
    )
    db.add(notion)

    competence = models.Competence(
        competence_id=uuid4(),
        referentiel_code="code_inexistant",
        title="placeholder title",
        description="",
        level="basique",
        notion_id=notion.notion_id,
    )
    db.add(competence)
    db.commit()

    with pytest.raises(ValueError):
        backfill_competence_descriptions(db)

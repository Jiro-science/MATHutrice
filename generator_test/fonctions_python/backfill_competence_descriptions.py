"""
backfill_competence_descriptions.py — Ajoute et peuple Competence.description
depuis REFERENTIEL.

Script one-off : ajoute la colonne competence.description si elle n'existe
pas encore (aucun outil de migration dans ce projet), puis pour chaque
Competence en base, retrouve l'entrée REFERENTIEL correspondante via
referentiel_code et copie son "nom" dans description.

C'est un point d'écriture : une Competence en base sans entrée REFERENTIEL
correspondante fait échouer le script plutôt que d'être sautée en silence
(docs/adr/0002-referentiel-db-divergence-policy.md).

Usage :
  python -m fonctions_python.backfill_competence_descriptions
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from sqlmodel import Session, select

from fonctions_python.main import REFERENTIEL
import models


def build_code_to_description() -> dict[str, str]:
    """Aplatit REFERENTIEL en un mapping referentiel_code -> nom."""
    return {
        competence["code"]: competence["nom"]
        for notion_data in REFERENTIEL.values()
        for competence in notion_data["competences"]
    }


def ensure_description_column(db: Session) -> None:
    """
    Ajoute la colonne competence.description en base si elle n'existe pas encore.

    SQLModel.metadata.create_all() ne crée que les tables manquantes ; il ne
    fait jamais d'ALTER TABLE sur une table déjà existante. Ce projet n'a pas
    d'outil de migration (Alembic), donc ce script porte lui-même ce DDL,
    idempotent (IF NOT EXISTS) pour pouvoir être rejoué sans risque.

    La colonne est ajoutée nullable : le NOT NULL du modèle SQLModel n'est
    posé qu'après le backfill (voir enforce_description_not_null), une fois
    qu'aucune ligne n'a de valeur manquante.
    """
    db.execute(text("ALTER TABLE competence ADD COLUMN IF NOT EXISTS description TEXT"))
    db.commit()


def enforce_description_not_null(db: Session) -> None:
    """Aligne la contrainte DB sur Competence.description: str (non nullable)."""
    db.execute(text("ALTER TABLE competence ALTER COLUMN description SET NOT NULL"))
    db.commit()


def backfill_competence_descriptions(db: Session) -> int:
    """
    Renseigne Competence.description pour chaque ligne dont le referentiel_code
    a une entrée REFERENTIEL correspondante, puis commit.

    Lève ValueError si une Competence en base n'a pas d'entrée REFERENTIEL.
    Retourne le nombre de lignes mises à jour.
    """
    code_to_description = build_code_to_description()
    competences = db.exec(select(models.Competence)).all()

    updated = 0
    for competence in competences:
        description = code_to_description.get(competence.referentiel_code)
        if description is None:
            raise ValueError(
                f"Competence.referentiel_code={competence.referentiel_code!r} "
                "n'a pas d'entrée correspondante dans REFERENTIEL."
            )
        competence.description = description
        db.add(competence)
        updated += 1

    db.commit()
    return updated


if __name__ == "__main__":
    from database import engine

    with Session(engine) as session:
        ensure_description_column(session)
        count = backfill_competence_descriptions(session)
        enforce_description_not_null(session)
        print(f"{count} compétence(s) mise(s) à jour.")

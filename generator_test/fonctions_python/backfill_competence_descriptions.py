"""
backfill_competence_descriptions.py — Peuple Competence.description depuis REFERENTIEL.

Script one-off : pour chaque Competence en base, retrouve l'entrée REFERENTIEL
correspondante via referentiel_code et copie son "nom" dans description.

C'est un point d'écriture : une Competence en base sans entrée REFERENTIEL
correspondante fait échouer le script plutôt que d'être sautée en silence
(docs/adr/0002-referentiel-db-divergence-policy.md).

Usage :
  python -m fonctions_python.backfill_competence_descriptions
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
        count = backfill_competence_descriptions(session)
        print(f"{count} compétence(s) mise(s) à jour.")

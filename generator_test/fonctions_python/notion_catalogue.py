"""
notion_catalogue.py — Lecture partagée et database-backed du catalogue Notion/Competence.

Deux fonctions publiques :
  - get_notion(notion_key, db)        : structure complète d'une notion (avec ses compétences),
                                         lève UnknownNotionKeyError si la clé est inconnue
  - get_competence_catalogue(db)      : catalogue plat de toutes les compétences (pas de clé
                                         en entrée, donc rien à faire échouer sur une clé inconnue)

Chaque compétence (et chaque notion) expose `title` (court, affiché à l'élève) et
`description` (long, utilisé pour construire les prompts de génération) comme deux
champs distincts — jamais l'un à la place de l'autre. Chaque entrée du catalogue
plat porte aussi `notion_title` (le titre court de sa notion d'appartenance), pour
permettre aux consommateurs de qualifier une compétence par sa notion sans requête
supplémentaire.

Ce module ne lit jamais REFERENTIEL : il lit uniquement les tables `Notion` et
`Competence`. Voir CONTEXT.md pour le vocabulaire Notion/Competence/referentiel_key.
"""

from dataclasses import dataclass

from sqlmodel import Session, select

import models


class UnknownNotionKeyError(LookupError):
    """Aucune Notion en base ne correspond à la referentiel_key demandée."""


@dataclass(frozen=True)
class CompetenceCatalogueEntry:
    code: str
    title: str
    description: str
    level: str
    notion_title: str


@dataclass(frozen=True)
class NotionCatalogue:
    key: str
    title: str
    description: str
    competences: list[CompetenceCatalogueEntry]


def _to_competence_entry(
    competence: models.Competence, notion_title: str
) -> CompetenceCatalogueEntry:
    return CompetenceCatalogueEntry(
        code=competence.referentiel_code,
        title=competence.title,
        description=competence.description,
        level=competence.level,
        notion_title=notion_title,
    )


def get_notion(notion_key: str, db: Session) -> NotionCatalogue:
    """
    Retourne la structure complète d'une notion (title, description, compétences)
    à partir de sa referentiel_key.

    Lève UnknownNotionKeyError si aucune Notion en base ne correspond à cette clé.
    """
    notion = db.exec(
        select(models.Notion).where(models.Notion.referentiel_key == notion_key)
    ).first()

    if notion is None:
        raise UnknownNotionKeyError(
            f"Aucune notion en base pour la referentiel_key {notion_key!r}"
        )

    competences = db.exec(
        select(models.Competence).where(models.Competence.notion_id == notion.notion_id)
    ).all()

    return NotionCatalogue(
        key=notion.referentiel_key,
        title=notion.title,
        description=notion.description,
        competences=[_to_competence_entry(c, notion.title) for c in competences],
    )


def get_competence_catalogue(db: Session) -> list[CompetenceCatalogueEntry]:
    """Retourne le catalogue plat de toutes les compétences, toutes notions confondues."""
    rows = db.exec(
        select(models.Competence, models.Notion.title).join(
            models.Notion, models.Competence.notion_id == models.Notion.notion_id
        )
    ).all()
    return [
        _to_competence_entry(competence, notion_title)
        for competence, notion_title in rows
    ]

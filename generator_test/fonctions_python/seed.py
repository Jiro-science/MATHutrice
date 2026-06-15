import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import engine
from sqlmodel import Session, SQLModel
import models

# Crée toutes les tables
SQLModel.metadata.create_all(engine)

notions = [
    (
        "trigonometrie",
        "Trigonométrie",
        "Étude des fonctions trigonométriques, des angles et du cercle trigonométrique.",
    ),
    (
        "fractions_puissances_radicaux",
        "Fractions – Puissances – Radicaux",
        "Manipulation des fractions, puissances et radicaux.",
    ),
    (
        "logarithme_exponentielle",
        "Logarithme et exponentielle",
        "Étude des fonctions logarithme et exponentielle.",
    ),
    (
        "manipulation_expressions_litterales",
        "Manipulation d'expressions littérales",
        "Isolement et manipulation de variables dans des expressions algébriques.",
    ),
    (
        "equations_inequations",
        "Équations – Inéquations",
        "Résolution d'équations et d'inéquations du premier et second degré.",
    ),
    (
        "polynomes_factorisation",
        "Polynômes – Factorisation",
        "Étude des polynômes, factorisation et identités remarquables.",
    ),
    (
        "analyse_dimensionnelle",
        "Analyse dimensionnelle",
        "Dimensions, unités et homogénéité des formules physiques.",
    ),
]

with Session(engine) as session:
    for notion_id, title, description in notions:
        existing = session.get(models.Notion, notion_id)
        if not existing:
            notion = models.Notion(
                notion_id=notion_id,
                title=title,
                description=description,
            )
            session.add(notion)
    session.commit()
    print("Notions insérées.")

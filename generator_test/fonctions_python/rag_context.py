"""
rag_context.py — Point d'entrée unique vers ChromaDB
=====================================================
Importé par tous les générateurs pour récupérer
le contexte RAG sans dupliquer le code.

Emplacement : generator_test/fonctions_python/rag_context.py

Usage dans n'importe quel générateur :
    from rag_context import get_exercices_context
    ctx = get_exercices_context("trigonométrie", "débutant")
    prompt = build_prompt(notion, niveau, ctx)
"""

import re
import sys
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# TYPES D'EXERCICES PAR NOTION
# (pour forcer la diversité dans les prompts Mistral)
# ══════════════════════════════════════════════════════

_TYPES_PAR_NOTION = {
    "trigonométrie": [
        "calculer une valeur exacte (sin, cos, tan)",
        "résoudre une équation trigonométrique",
        "simplifier une expression trigonométrique",
        "linéariser une expression (cos²x, sin²x...)",
        "utiliser les formules d'addition",
        "vérifier une identité trigonométrique",
    ],
    "fractions_puissances_radicaux": [
        "simplifier une fraction algébrique",
        "calculer avec des puissances négatives",
        "rationaliser un dénominateur avec radical",
        "simplifier une expression avec exposants fractionnaires",
        "additionner ou soustraire des fractions avec variable",
        "réduire une expression avec puissances et radicaux",
    ],
    "équations_inéquations": [
        "résoudre une équation du second degré",
        "résoudre une inéquation (tableau de signes)",
        "résoudre une équation-quotient (conditions d'existence)",
        "résoudre une équation-produit",
        "résoudre une inéquation-quotient",
        "résoudre un système de deux équations",
    ],
    "polynômes_factorisation": [
        "factoriser un trinôme du second degré",
        "étudier le signe d'un polynôme",
        "trouver les racines par le discriminant",
        "utiliser la forme canonique",
        "décomposer en facteurs irréductibles",
    ],
    "logarithme_exponentielle": [
        "résoudre une équation avec ln",
        "résoudre une équation avec exp",
        "simplifier une expression avec ln",
        "comparer des croissances (xⁿ, eˣ, ln(x))",
        "résoudre une inéquation avec logarithme",
    ],
    "expressions_littérales": [
        "développer avec la double distributivité",
        "factoriser par un facteur commun",
        "utiliser les identités remarquables",
        "simplifier une expression avec fractions littérales",
        "factoriser une expression complexe",
    ],
    "analyse_dimensionnelle": [
        "vérifier l'homogénéité d'une formule",
        "trouver les dimensions d'une grandeur inconnue",
        "exprimer une unité dérivée en unités de base",
    ],
}

_TYPES_DEFAUT = [
    "calcul direct avec application numérique",
    "démonstration ou vérification",
    "application à un cas concret",
    "simplification d'expression",
    "résolution d'équation ou inéquation",
]


# Tracker des types déjà utilisés dans la session courante
_types_utilises: dict[str, list] = {}


def get_type_aleatoire(notion: str) -> str:
    """
    Retourne un type d'exercice aléatoire NON RÉPÉTÉ.
    - Évite de retourner le même type 2 fois de suite
    - Rotation complète avant de recommencer
    """
    import random
    global _types_utilises

    types = list(_TYPES_PAR_NOTION.get(notion, _TYPES_DEFAUT))

    # Initialiser ou récupérer l'historique de la notion
    if notion not in _types_utilises:
        _types_utilises[notion] = []

    historique = _types_utilises[notion]

    # Filtrer les types déjà utilisés récemment
    # (garder au moins 1 option disponible)
    types_dispo = [t for t in types if t not in historique[-2:]]
    if not types_dispo:
        # Tous utilisés → réinitialiser la rotation
        _types_utilises[notion] = []
        types_dispo = types

    choix = random.choice(types_dispo)
    _types_utilises[notion].append(choix)

    # Garder seulement les N derniers dans l'historique
    _types_utilises[notion] = _types_utilises[notion][-(len(types)):]

    return choix


def reset_types_session(notion: str = None):
    """Réinitialise le tracker (nouveau test / nouvelle session)."""
    global _types_utilises
    if notion:
        _types_utilises[notion] = []
    else:
        _types_utilises = {}


# ── Chemins ────────────────────────────────────────────
# fonctions_python/rag_context.py
# → parent = fonctions_python/
# → parent.parent = generator_test/
# → generator_test/rag/ contient step3_vectorstore.py
_FONCTIONS_DIR = Path(__file__).resolve().parent
_ROOT          = _FONCTIONS_DIR.parent
_RAG_DIR       = _ROOT / "rag"

sys.path.insert(0, str(_RAG_DIR))


# ══════════════════════════════════════════════════════
# CHARGEMENT CHROMADB (singleton — chargé une seule fois)
# ══════════════════════════════════════════════════════

_store = None


def _get_store():
    """
    Charge ChromaDB une seule fois, le réutilise ensuite.
    Retourne None si ChromaDB est indisponible.
    """
    global _store
    if _store is not None:
        return _store

    try:
        # Import robuste : marche depuis la racine (rag/ dans sys.path)
        # ou en package (rag.step3_vectorstore)
        try:
            from step3_vectorstore import ChromaVectorStore, CHROMA_DIR
        except ImportError:
            from rag.step3_vectorstore import ChromaVectorStore, CHROMA_DIR
        store = ChromaVectorStore.load(chroma_dir=CHROMA_DIR)
        if len(store) == 0:
            logger.warning("Vectorstore vide — lancer run_pipeline_batch.py")
            return None
        _store = store
        logger.info(f"Vectorstore chargé : {len(store)} chunks")
        return _store
    except Exception as e:
        logger.warning(f"Vectorstore indisponible : {e}")
        return None


# ══════════════════════════════════════════════════════
# FORMATTAGE DU CONTEXTE
# ══════════════════════════════════════════════════════

def _format_chunks(chunks: list[dict], max_chars: int = 1200) -> str:
    """
    Formate une liste de chunks en texte lisible pour Mistral.
    Nettoie le LaTeX et limite la taille.
    """
    if not chunks:
        return ""

    parts = ["### Exercices de référence (extraits du cours) :\n"]
    total = 0

    for i, chunk in enumerate(chunks, 1):
        m    = chunk.get("metadata", {})
        diff = m.get("difficulte", "?")
        typ  = m.get("type_exercice", "") or ""
        text = chunk.get("text", "")[:400]

        # Nettoyer LaTeX → lisible par Mistral
        text = re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'(\1)/(\2)', text)
        text = re.sub(r'\\sqrt\{([^}]*)\}',             r'√(\1)',     text)
        text = re.sub(r'\$\$([^$]+)\$\$',               r'\1',        text)
        text = re.sub(r'\$([^$]+)\$',                    r'\1',        text)
        text = re.sub(r'\\[a-zA-Z]+',                    '',           text)
        text = re.sub(r'\[formule\]',                    '[...]',      text)

        entry = f"[Ref {i} | diff {diff}/5 | {typ}]\n{text.strip()}\n\n"
        if total + len(entry) > max_chars:
            break

        parts.append(entry)
        total += len(entry)

    return "".join(parts) if len(parts) > 1 else ""


def _format_cours(chunks: list[dict], max_chars: int = 1500) -> str:
    """
    Formate des chunks de cours en texte lisible pour Mistral.
    """
    if not chunks:
        return ""

    parts = ["### Extrait du cours :\n"]
    total = 0

    for chunk in chunks:
        text  = chunk.get("text", "")[:500]
        text  = re.sub(r'\$\$([^$]+)\$\$', r'\1', text)
        text  = re.sub(r'\$([^$]+)\$',      r'\1', text)
        text  = re.sub(r'\\[a-zA-Z]+',       '',    text)
        text  = re.sub(r'\[formule\]',        '[...]', text)

        entry = f"{text.strip()}\n\n"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    return "".join(parts) if len(parts) > 1 else ""


# ══════════════════════════════════════════════════════
# FONCTIONS PUBLIQUES
# ══════════════════════════════════════════════════════

def get_exercices_context(notion: str, niveau: str, k: int = 3) -> str:
    """
    Récupère des exercices similaires depuis ChromaDB.
    Utilisé par : qcm_generator, qro_generator, steps_generator, trous_generator

    Args:
        notion : ex "trigonométrie"
        niveau : "débutant" | "intermédiaire" | "avancé"
        k      : nombre d'exercices à récupérer (défaut 3)

    Returns:
        Texte formaté à injecter dans le prompt Mistral.
        Chaîne vide si ChromaDB indisponible → génération sans RAG.
    """
    store = _get_store()
    if store is None:
        return ""

    diff_max = {"débutant": 2, "intermédiaire": 3, "avancé": 5}.get(niveau, 3)

    # Recherche avec filtres
    # ── Stratégie 1 : MMR via LangChain retriever ──────
    # MMR diversifie les chunks retournés : évite 3 chunks
    # quasi-identiques sur le même exercice
    try:
        retriever = store.as_langchain_retriever(
            k           = k * 3,     # récupère plus pour MMR
            search_type = "mmr",     # Maximal Marginal Relevance
            filters     = {"notion": notion}
        )
        docs = retriever.invoke(notion)
        # Reconstruire le format {chunk, score} attendu
        raw_results = [
            {"chunk": {"text": d.page_content, "metadata": dict(d.metadata)},
             "score": 1.0}
            for d in docs
        ]
    except Exception:
        # Fallback : recherche classique si MMR indisponible
        try:
            raw_results = store.search(notion, k=k * 3,
                                       filters={"notion": notion})
        except Exception:
            raw_results = []

    # ── Filtrage Python : niveau + doc_type ─────────────
    filtered = [
        r for r in raw_results
        if (r["chunk"] if isinstance(r, dict) else r.chunk)
           .get("metadata", {}).get("difficulte", 99) <= diff_max
        and (r["chunk"] if isinstance(r, dict) else r.chunk)
           .get("metadata", {}).get("doc_type", "exercices") == "exercices"
    ]

    # ── Stratégie 3 : Rotation aléatoire des chunks ─────
    # Au lieu de toujours prendre les k premiers,
    # on tire au hasard parmi les résultats disponibles
    import random
    if len(filtered) > k:
        results = random.sample(filtered, k)
    elif filtered:
        results = filtered
    else:
        # Fallback sans filtre + rotation
        try:
            fallback = store.search(notion, k=k * 2)
            results  = random.sample(fallback, min(k, len(fallback)))
        except Exception:
            results = []

    chunks = [
        r["chunk"] if isinstance(r, dict) else r.chunk
        for r in results
    ]
    return _format_chunks(chunks)


def get_exercices_context_competence(notion_nom: str, competence: dict,
                                     k: int = 3) -> str:
    """
    Version par COMPÉTENCE (nouvelle API).
    Récupère des exercices ChromaDB ciblés sur une compétence précise.

    Args:
        notion_nom : ex "Trigonométrie"
        competence : {"code", "nom", "niveau", "score"}
        k          : nombre d'exercices

    Returns:
        Texte formaté pour le prompt Mistral.
    """
    # Mapper le niveau compétence → niveau ChromaDB
    niveau_map = {"basique": "débutant", "solide": "intermédiaire",
                  "expert": "avancé"}
    niveau = niveau_map.get(competence.get("niveau", ""), "intermédiaire")

    # Requête enrichie : notion + nom de la compétence pour affiner
    query = f"{notion_nom} {competence.get('nom', '')}"

    store = _get_store()
    if store is None:
        return ""

    diff_max = {"débutant": 2, "intermédiaire": 3, "avancé": 5}.get(niveau, 3)

    # MMR pour diversité
    try:
        retriever = store.as_langchain_retriever(
            k           = k * 3,
            search_type = "mmr",
            filters     = {"notion": notion_nom},
        )
        docs = retriever.invoke(query)
        raw_results = [
            {"chunk": {"text": d.page_content, "metadata": dict(d.metadata)},
             "score": 1.0}
            for d in docs
        ]
    except Exception:
        try:
            raw_results = store.search(query, k=k * 3,
                                       filters={"notion": notion_nom})
        except Exception:
            raw_results = []

    # Filtrer par difficulté
    filtered = [
        r for r in raw_results
        if (r["chunk"] if isinstance(r, dict) else r.chunk)
           .get("metadata", {}).get("difficulte", 99) <= diff_max
    ]

    # Rotation aléatoire
    import random
    if len(filtered) > k:
        results = random.sample(filtered, k)
    elif filtered:
        results = filtered
    else:
        try:
            fb = store.search(query, k=k)
            results = fb
        except Exception:
            results = []

    chunks = [r["chunk"] if isinstance(r, dict) else r.chunk for r in results]
    return _format_chunks(chunks)


def get_cours_context(notion: str, k: int = 2) -> str:
    """
    Récupère des extraits de cours depuis ChromaDB.
    Utilisé par : explications, tuteur conversationnel

    Args:
        notion : ex "trigonométrie"
        k      : nombre de chunks de cours à récupérer

    Returns:
        Texte formaté à injecter dans le prompt Mistral.
    """
    store = _get_store()
    if store is None:
        return ""

    try:
        results = store.search(
            query   = notion,
            k       = k * 2,
            filters = {"notion": notion}
        )
        # Filtrer doc_type=cours en Python
        cours = [
            r for r in results
            if (r["chunk"] if isinstance(r, dict) else r.chunk)
               .get("metadata", {}).get("doc_type", "") == "cours"
        ][:k]
        results = cours if cours else results[:k]
    except Exception:
        results = []

    # Fallback sans filtre
    if not results:
        try:
            results = store.search(notion, k=k)
        except Exception:
            results = []

    chunks = [
        r["chunk"] if isinstance(r, dict) else r.chunk
        for r in results
    ]
    return _format_cours(chunks)


def get_full_context(notion: str, niveau: str) -> str:
    """
    Récupère exercices + cours depuis ChromaDB.
    Utilisé par : tuteur conversationnel, explications enrichies

    Returns:
        Texte combiné cours + exercices.
    """
    cours_ctx = get_cours_context(notion, k=1)
    exos_ctx  = get_exercices_context(notion, niveau, k=2)

    parts = []
    if cours_ctx:
        parts.append(cours_ctx)
    if exos_ctx:
        parts.append(exos_ctx)
    return "\n".join(parts)


def is_available() -> bool:
    """Retourne True si ChromaDB est chargé et utilisable."""
    return _get_store() is not None


def stats() -> dict:
    """Stats ChromaDB — utile pour le debug."""
    store = _get_store()
    if store is None:
        return {"status": "indisponible"}
    return store.stats()


# ══════════════════════════════════════════════════════
# TEST RAPIDE
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("="*55)
    print("  TEST rag_context.py")
    print("="*55)

    print(f"\n  ChromaDB disponible : {is_available()}")

    if is_available():
        s = stats()
        print(f"  Chunks total       : {s.get('total_chunks', 0)}")
        print(f"  Notions indexées   : {s.get('notions', [])}")

        notion = s.get('notions', ['trigonométrie'])[0]
        print(f"\n  Test get_exercices_context('{notion}', 'intermédiaire') :")
        ctx = get_exercices_context(notion, "intermédiaire")
        if ctx:
            print(ctx[:300])
            print("  ...")
        else:
            print("  (vide)")

        print(f"\n  Test get_cours_context('{notion}') :")
        ctx_cours = get_cours_context(notion)
        if ctx_cours:
            print(ctx_cours[:300])
        else:
            print("  (vide — pas de cours indexés)")
    else:
        print("\n  ⚠  Lancer d'abord : python run_pipeline_batch.py --dataset ./dataset")

    print("="*55)

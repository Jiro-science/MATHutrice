"""
PIPELINE RAG COMPLET — MATHutrice
===================================
Orchestrateur des 4 étapes :
  1. Chunking atomique (sous-question par chunk)
  2. Enrichissement LLM des métadonnées
  3. Vectorisation (TF-IDF local / ChromaDB en prod)
  4. Démonstration du retriever pédagogique

Usage :
  python run_pipeline.py              # pipeline complet
  python run_pipeline.py --step 1     # étape 1 seulement
  python run_pipeline.py --step 3     # depuis étape 3 (réutilise chunks_enrichis.json)
  python run_pipeline.py --no-llm     # skip enrichissement LLM (utilise heuristiques)
"""

import sys
import time
import json
import argparse
from pathlib import Path

# Ajouter le dossier pipeline au path
_PROJECT_ROOT = Path(__file__).resolve().parent
_RAG_DIR = _PROJECT_ROOT / 'rag'
sys.path.insert(0, str(_RAG_DIR))  # step1, step2, step3 sont dans rag/

OUT_DIR = Path(__file__).resolve().parent / 'rag' / 'output'
OUT_DIR.mkdir(parents=True, exist_ok=True)
_PROJECT_ROOT = Path(__file__).resolve().parent
PDF_PATH = str(_PROJECT_ROOT / 'dataset' / 'Bases_fractions_puiss_radicaux.pdf')


def run_full_pipeline(start_step: int = 1, use_llm: bool = True):
    t_total = time.perf_counter()

    print("\n" + "═" * 62)
    print("  🎓  PIPELINE RAG — MATHutrice")
    print("  Fractions · Puissances · Radicaux — L1")
    print("═" * 62 + "\n")

    # ══ ÉTAPE 1 : Chunking atomique ══════════════════════
    if start_step <= 1:
        print("┌─ ÉTAPE 1/4 : Chunking atomique ──────────────────────┐")
        from step1_docling_chunker import run as run_step1
        chunks = run_step1(PDF_PATH)
        print(f"└── {len(chunks)} chunks atomiques créés\n")
    else:
        print("↩  Étape 1 : chargement depuis chunks_atomiques.json")
        with open(OUT_DIR / 'chunks_atomiques.json', 'r') as f:
            chunks = json.load(f)
        print(f"   {len(chunks)} chunks chargés\n")

    # ══ ÉTAPE 2 : Enrichissement LLM ═════════════════════
    if start_step <= 2:
        print("┌─ ÉTAPE 2/4 : Enrichissement LLM ─────────────────────┐")
        from step2_llm_enricher import run as run_step2, enrich_chunks
        if use_llm:
            chunks = run_step2(OUT_DIR / 'chunks_atomiques.json', delay=0.4)
        else:
            print("  Mode --no-llm : enrichissement heuristique uniquement")
            from step2_llm_enricher import (
                _fallback_concept, _fallback_difficulty,
                _fallback_type, _fallback_prereqs
            )
            for c in chunks:
                txt = c['text']
                c['metadata']['concept']       = _fallback_concept(txt)
                c['metadata']['difficulte']    = _fallback_difficulty(txt)
                c['metadata']['type_exercice'] = _fallback_type(txt)
                c['metadata']['prerequis']     = _fallback_prereqs(txt)
                c['metadata']['llm_enriched']  = False
            out = OUT_DIR / 'chunks_enrichis.json'
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(chunks, f, ensure_ascii=False, indent=2)
            print(f"  {len(chunks)} chunks enrichis (heuristiques) → {out}")
        llm_ok = sum(1 for c in chunks if c['metadata'].get('llm_enriched'))
        print(f"└── {llm_ok}/{len(chunks)} chunks enrichis par LLM\n")
    else:
        print("↩  Étape 2 : chargement depuis chunks_enrichis.json")
        with open(OUT_DIR / 'chunks_enrichis.json', 'r') as f:
            chunks = json.load(f)
        print(f"   {len(chunks)} chunks chargés\n")

    # ══ ÉTAPE 3 : Vectorisation ═══════════════════════════
    if start_step <= 3:
        print("┌─ ÉTAPE 3/4 : Vectorisation ───────────────────────────┐")
        from step3_vectorstore import run as run_step3
        store = run_step3(OUT_DIR / 'chunks_enrichis.json')
        print(f"└── {len(store)} chunks vectorisés\n")
    else:
        print("↩  Étape 3 : chargement du vectorstore existant")
        try:
            from embeddings import SemanticVectorStore
            from step3_vectorstore import STORE_PATH
            store = SemanticVectorStore.load(STORE_PATH)
        except ImportError:
            from step3_vectorstore import LocalVectorStore, STORE_PATH
            store = LocalVectorStore.load(STORE_PATH)
        print(f"   {len(store)} chunks chargés\n")

    # ══ ÉTAPE 4 : Démo retriever ══════════════════════════
    print("┌─ ÉTAPE 4/4 : Démonstration du retriever ──────────────┐")
    from step4_retriever import PedagoRetriever, EleveProfil

    eleve = EleveProfil(
        eleve_id           = "demo_alice",
        nom                = "Alice",
        niveau             = 2,
        notions_maitrisees = ["fractions simples"],
        lacunes            = ["fractions algébriques", "puissances négatives"],
        score_moyen        = 0.61,
        nb_exercices       = 8
    )
    retriever = PedagoRetriever(store, eleve)

    # Résumé des capacités
    print(f"  Retriever initialisé pour : {eleve.nom} (niveau {eleve.niveau}/5)")
    print(f"  Lacunes suivies : {eleve.lacunes}")

    # Test rapide
    q    = "Comment simplifier une fraction avec des puissances ?"
    ctx  = retriever.get_context_for_question(q, k=3)
    exos = retriever.get_exercices_entrainement(k=3)
    eval_q = retriever.get_exercices_evaluation(nb_questions=3)

    print(f"\n  Requête test : \"{q}\"")
    print(f"  → {len(ctx)} chunk(s) de contexte récupérés")
    print(f"  → {len(exos)} exercice(s) d'entraînement")
    print(f"  → {len(eval_q)} question(s) d'évaluation")

    if ctx:
        print(f"\n  Meilleur chunk récupéré :")
        c = ctx[0]; m = c['metadata']
        print(f"    ID      : {c['id']}")
        print(f"    Concept : {m.get('concept','?')}")
        print(f"    Diff    : {m.get('difficulte','?')}/5")
        print(f"    Type    : {m.get('type_exercice','?')}")
        print(f"    Texte   : {c['text'][:200]}...")

    print(f"└── Retriever opérationnel ✓\n")

    # ══ RAPPORT FINAL ═════════════════════════════════════
    elapsed = time.perf_counter() - t_total

    print("═" * 62)
    print("  📊  RAPPORT FINAL")
    print("═" * 62)
    print(f"  ⏱  Temps total               : {elapsed:.1f}s")
    print(f"  📦  Chunks atomiques          : {len(chunks)}")
    valid_diff = [c['metadata']['difficulte'] for c in chunks
                  if isinstance(c['metadata'].get('difficulte'), int)
                  and c['metadata']['difficulte'] > 0]
    if valid_diff:
        avg_d = sum(valid_diff) / len(valid_diff)
        print(f"  📐  Difficulté moy. (LLM)    : {avg_d:.1f}/5")
    llm_ok = sum(1 for c in chunks if c['metadata'].get('llm_enriched'))
    print(f"  🤖  Enrichis par LLM          : {llm_ok}/{len(chunks)}")
    print(f"  🖼   Avec images formule       : {sum(1 for c in chunks if c.get('formula_images'))}")
    print()

    # Distribution des difficultés
    from collections import Counter
    dist = Counter(c['metadata'].get('difficulte') for c in chunks
                   if isinstance(c['metadata'].get('difficulte'), int))
    print("  Distribution difficulté :")
    for d in sorted(dist):
        bar  = '█' * dist[d]
        star = '★' * d + '☆' * (5-d) if d else '—'
        print(f"    [{star}] {dist[d]:2d} chunks  {bar}")

    # Distribution des types
    types = Counter(c['metadata'].get('type_exercice') for c in chunks
                    if c['metadata'].get('type_exercice'))
    print("\n  Types d'exercices :")
    for t, n in types.most_common():
        print(f"    {t:20s} : {n}")

    print("\n  Fichiers générés :")
    for f in [OUT_DIR/'chunks_atomiques.json',
              OUT_DIR/'chunks_enrichis.json',
              OUT_DIR/'vectorstore.pkl']:
        if f.exists():
            print(f"    ✓ {f}  ({f.stat().st_size // 1024} KB)")

    print("\n  🔌  Intégration LangChain :")
    print("    lc_retriever = retriever.as_langchain_retriever(k=4)")
    print("    chain = ConversationalRetrievalChain.from_llm(llm, lc_retriever)")
    print()
    print("═" * 62)
    print("  Pipeline RAG MATHutrice — prêt pour production")
    print("═" * 62 + "\n")

    return retriever


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Pipeline RAG MATHutrice')
    parser.add_argument('--step',   type=int, default=1,
                        help='Étape de départ (1-4)')
    parser.add_argument('--no-llm', action='store_true',
                        help='Utiliser les heuristiques au lieu du LLM')
    args = parser.parse_args()

    run_full_pipeline(
        start_step = args.step,
        use_llm    = not args.no_llm
    )

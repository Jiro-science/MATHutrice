"""
step3_vectorstore.py — BDD vectorielle ChromaDB + LangChain
============================================================
Version ChromaDB (développement local).

Pour migrer vers pgvector plus tard : remplacer ce fichier par
la version pgvector — l'interface est identique (aliases garantis).

Stockage : rag/output/chromadb/

Installation :
    pip install langchain-chroma chromadb langchain-huggingface

Emplacement : generator_test/rag/step3_vectorstore.py
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Chemins ────────────────────────────────────────────
_THIS_DIR  = Path(__file__).resolve().parent
OUT_DIR    = _THIS_DIR / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CHROMA_DIR = OUT_DIR / "chromadb"
STORE_PATH = OUT_DIR / "vectorstore.pkl"   # conservé pour compatibilité

COLLECTION_NAME = "mathutrice_rag"


# ══════════════════════════════════════════════════════════
# EMBEDDINGS
# ══════════════════════════════════════════════════════════

def _get_langchain_embeddings():
    """Solon FR (sentence-transformers)."""
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
        embeddings = HuggingFaceEmbeddings(
            model_name    = "OrdalieTech/Solon-embeddings-large-0.1",
            model_kwargs  = {"device": "cpu"},
            encode_kwargs = {"normalize_embeddings": True},
        )
        logger.info("Embeddings : Solon FR (OrdalieTech)")
        return embeddings
    except Exception as e:
        logger.warning(f"Solon FR indisponible ({e}) → MiniLM fallback")
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(
            model_name="paraphrase-multilingual-MiniLM-L12-v2"
        )


# ══════════════════════════════════════════════════════════
# CONVERSION chunks ↔ Documents LangChain
# ══════════════════════════════════════════════════════════

def _chunks_to_documents(chunks: list[dict]):
    """Convertit nos chunks en Documents LangChain."""
    from langchain_core.documents import Document

    docs = []
    for c in chunks:
        m = c.get("metadata", {})
        # ChromaDB n'accepte que str/int/float/bool dans les métadonnées
        flat_meta = {
            "chunk_id"      : str(c.get("id", "")),
            "notion"        : str(m.get("notion", "")),
            "niveau"        : str(m.get("niveau", "")),
            "doc_type"      : str(m.get("doc_type", "exercices")),
            "pdf_source"    : str(m.get("pdf_source", "")),
            "page"          : int(m.get("page", 1)),
            "difficulte"    : int(m.get("difficulte", 3)),
            "type_exercice" : str(m.get("type_exercice", "")),
            "concept"       : str(m.get("concept", "") or ""),
            "has_latex"     : bool(m.get("has_latex", False)),
            "char_count"    : int(m.get("char_count", len(c.get("text", "")))),
            "llm_enriched"  : bool(m.get("llm_enriched", False)),
            "prerequis"     : json.dumps(m.get("prerequis", []),
                                         ensure_ascii=False),
        }
        docs.append(Document(
            page_content = c.get("text", ""),
            metadata     = flat_meta,
        ))
    return docs


def _document_to_chunk(doc) -> dict:
    """Reconvertit un Document LangChain en chunk interne."""
    m = dict(doc.metadata)
    try:
        prereq = json.loads(m.get("prerequis", "[]"))
    except Exception:
        prereq = []
    return {
        "id"  : m.get("chunk_id", ""),
        "text": doc.page_content,
        "metadata": {**m, "prerequis": prereq},
    }


# ══════════════════════════════════════════════════════════
# CLASSE PRINCIPALE — ChromaVectorStore
# ══════════════════════════════════════════════════════════

class ChromaVectorStore:
    """
    BDD vectorielle ChromaDB avec LangChain.
    Interface identique à la version pgvector — aucun autre
    fichier ne change lors d'une migration.
    """

    def __init__(self, chroma_dir=None, collection_name: str = COLLECTION_NAME):
        self.chroma_dir      = Path(chroma_dir) if chroma_dir else CHROMA_DIR
        self.collection_name = collection_name
        self._store          = None
        self._embeddings     = None

    def _init_store(self, embeddings=None):
        """Initialise ou charge le store ChromaDB."""
        from langchain_chroma import Chroma

        self._embeddings = embeddings or _get_langchain_embeddings()
        self.chroma_dir.mkdir(parents=True, exist_ok=True)

        self._store = Chroma(
            collection_name    = self.collection_name,
            embedding_function = self._embeddings,
            persist_directory  = str(self.chroma_dir),
        )
        return self._store

    # ── Propriété chunks ────────────────────────────────
    @property
    def chunks(self) -> list[dict]:
        """Retourne tous les chunks indexés."""
        if self._store is None:
            self._init_store()
        try:
            data = self._store.get()
            docs = []
            for i, text in enumerate(data.get("documents", [])):
                meta = data.get("metadatas", [{}])[i] if i < len(data.get("metadatas", [])) else {}
                from langchain_core.documents import Document
                docs.append(Document(page_content=text, metadata=meta))
            return [_document_to_chunk(d) for d in docs]
        except Exception:
            return []

    def __len__(self) -> int:
        """Nombre de chunks indexés."""
        if self._store is None:
            self._init_store()
        try:
            return self._store._collection.count()
        except Exception:
            try:
                return len(self._store.get().get("documents", []))
            except Exception:
                return 0

    # ── Ajout ───────────────────────────────────────────
    def add(self, chunks: list[dict], embeddings=None):
        """Indexe les chunks dans ChromaDB."""
        import time
        if not chunks:
            return

        if self._store is None:
            self._init_store(embeddings)

        print(f"  ChromaDB : indexation de {len(chunks)} chunks...")
        t0   = time.perf_counter()
        docs = _chunks_to_documents(chunks)
        ids  = [str(c.get("id", f"chunk_{i}")) for i, c in enumerate(chunks)]

        self._store.add_documents(documents=docs, ids=ids)
        dt = time.perf_counter() - t0
        print(f"  ChromaDB : ✓ {len(chunks)} chunks indexés en {dt:.1f}s")

    # ── Recherche ────────────────────────────────────────
    def search(self, query: str, k: int = 5,
               filters: dict = None) -> list[dict]:
        """
        Recherche sémantique avec filtres optionnels.

        Filtres ChromaDB (where) :
            {"notion": "trigonométrie"}
            {"difficulte": {"$lte": 3}}
        """
        if self._store is None:
            self._init_store()

        try:
            kwargs = {"k": k}
            if filters:
                kwargs["filter"] = filters

            docs_scores = self._store.similarity_search_with_score(query, **kwargs)
            results = []
            for doc, score in docs_scores:
                chunk      = _document_to_chunk(doc)
                # ChromaDB retourne une distance → convertir en similarité
                similarity = max(0.0, 1.0 - score)
                results.append({"chunk": chunk, "score": similarity})
            return results
        except Exception as e:
            logger.error(f"ChromaDB search error: {e}")
            return []

    def get_by_id(self, chunk_id: str) -> Optional[dict]:
        """Récupère un chunk par son ID."""
        if self._store is None:
            self._init_store()
        try:
            data = self._store.get(ids=[chunk_id])
            if data.get("documents"):
                from langchain_core.documents import Document
                doc = Document(
                    page_content = data["documents"][0],
                    metadata     = data["metadatas"][0],
                )
                return _document_to_chunk(doc)
        except Exception:
            pass
        return None

    # ── LangChain natif (MMR) ────────────────────────────
    def as_langchain_retriever(self, k: int = 4,
                                search_type: str = "mmr",
                                filters: dict = None):
        """Retourne un retriever LangChain natif (MMR pour la diversité)."""
        if self._store is None:
            self._init_store()

        search_kwargs = {"k": k}
        if filters:
            search_kwargs["filter"] = filters

        return self._store.as_retriever(
            search_type   = search_type,
            search_kwargs = search_kwargs,
        )

    # ── Sauvegarde / Chargement ──────────────────────────
    def save(self, path=None):
        """ChromaDB persiste automatiquement (persist_directory)."""
        logger.debug("ChromaDB : persistance automatique")

    @classmethod
    def load(cls, path=None, chroma_dir=None,
             collection_name: str = COLLECTION_NAME) -> "ChromaVectorStore":
        """Charge le store ChromaDB existant."""
        store = cls(chroma_dir=chroma_dir, collection_name=collection_name)
        store._init_store()
        try:
            count = len(store)
            print(f"  ChromaDB chargé : {count} chunks "
                  f"(collection '{collection_name}')")
        except Exception as e:
            logger.warning(f"ChromaDB : erreur chargement — {e}")
        return store

    # ── Stats ────────────────────────────────────────────
    def stats(self) -> dict:
        """Stats pour le dashboard admin."""
        from collections import Counter
        chunks = self.chunks
        if not chunks:
            return {"status": "empty", "total_chunks": 0}

        return {
            "status"           : "ready",
            "total_chunks"     : len(chunks),
            "notions"          : list(set(
                c["metadata"].get("notion", "") for c in chunks
            )),
            "difficulte_dist"  : dict(Counter(
                c["metadata"].get("difficulte") for c in chunks
            )),
            "types"            : dict(Counter(
                c["metadata"].get("type_exercice", "") for c in chunks
            )),
            "llm_enriched_pct" : round(
                sum(1 for c in chunks if c["metadata"].get("llm_enriched"))
                / len(chunks) * 100
            ) if chunks else 0,
            "backend"          : "chromadb",
        }


# ══════════════════════════════════════════════════════════
# ALIASES — compatibilité avec le reste du code
# ══════════════════════════════════════════════════════════

PGVectorStore       = ChromaVectorStore
LocalVectorStore    = ChromaVectorStore
SemanticVectorStore = ChromaVectorStore


# ══════════════════════════════════════════════════════════
# FONCTION run() — point d'entrée step3
# ══════════════════════════════════════════════════════════

def run(chunks: list[dict]) -> ChromaVectorStore:
    """Indexe les chunks dans ChromaDB. Appelé par run_pipeline_batch.py."""
    store = ChromaVectorStore()
    store.add(chunks)
    return store


if __name__ == "__main__":
    print("ChromaDB store — test connexion")
    try:
        store = ChromaVectorStore.load()
        print(store.stats())
    except Exception as e:
        print(f"Erreur : {e}")
        print("Vérifier : pip install langchain-chroma chromadb")

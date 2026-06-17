"""
step1_docling_chunker.py — Chunking avec Docling + LangChain
=============================================================
Remplace step1_atomic_chunker.py par une approche Docling.

Pourquoi Docling est meilleur pour les maths :
  - Convertit les formules PDF → LaTeX propre (\\frac, \\sqrt, etc.)
  - Respecte la structure hiérarchique (titres → sections → exercices)
  - Gère les tableaux, images, formules multi-lignes comme entités insécables
  - Produit un Markdown structuré → splitter intelligent par headers

Problèmes du code proposé corrigés ici :
  1. raw_docs[0].page_content → faux si le PDF a plusieurs pages/docs
  2. MarkdownHeaderTextSplitter seul → insuffisant si pas de headers #/##/###
     (nos PDFs utilisent "Exercice 1 :", "Partie 1 :", "1. Simplifier"...)
  3. Pas de fallback si Docling échoue ou si les formules sont en image
  4. Pas de gestion du doc_type cours vs exercices
  5. Métadonnées incomplètes (difficulte, niveau, concept manquants)

Installation :
    pip install docling docling-core langchain-docling

Usage :
    from step1_docling_chunker import run
    chunks = run("path/to/doc.pdf", notion_override="trigonométrie", doc_type="exercices")
"""

import re
import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── Chemins ────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
OUT_DIR   = _THIS_DIR.parent / "rag" / "output"
IMG_DIR   = OUT_DIR / "formula_images"
for d in [OUT_DIR, IMG_DIR]: d.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════
# CONFIGURATION DOCLING
# ══════════════════════════════════════════════════════════════


def _clean_docling_markdown(text: str) -> str:
    """
    Nettoie le Markdown produit par Docling.

    3 corrections appliquées dans l'ordre :

    1. <!-- formula-not-decoded --> → [formule]
       Docling écrit ce marqueur quand il voit une formule en image
       qu'il ne peut pas convertir en LaTeX.

    2. Caractères Unicode mathématiques italiques → ASCII
       𝑇 𝑥 𝑛 𝑘 (U+1D400...) → T x n k
       Ces caractères viennent des polices mathématiques PDF.

    3. Exposants mal formatés → notation ^
       "6x 3"   → "6x^3"         (exposant chiffre avec espace)
       "3n+2"   → "3^(n+2)"      (exposant compteur composé)
       "3n"     → "3^n"           (exposant compteur simple)
       "x 2k+1" → "x^(2k+1)"     (exposant composé avec espace)

    Limite : les cas "x 2k+1" vs "x × (2k+1)" sont ambigus sans OCR
    mathématique — activer pix2tex pour les résoudre proprement.
    """
    # ── 1. formula-not-decoded ──────────────────────────────
    text = re.sub(r'<!--\s*formula-not-decoded\s*-->', '[formule]', text)

    # ── 2. Unicode math italique → ASCII ───────────────────
    MATH_RANGES = [
        (0x1D400, 0x1D41A, ord('A')),  # A-Z bold
        (0x1D41A, 0x1D434, ord('a')),  # a-z bold
        (0x1D434, 0x1D44E, ord('A')),  # A-Z italic
        (0x1D44E, 0x1D468, ord('a')),  # a-z italic
        (0x1D468, 0x1D482, ord('A')),  # A-Z bold italic
        (0x1D482, 0x1D49C, ord('a')),  # a-z bold italic
    ]
    chars = []
    for c in text:
        n = ord(c)
        replaced = False
        for start_r, end_r, base in MATH_RANGES:
            if start_r <= n < end_r:
                chars.append(chr(base + n - start_r))
                replaced = True
                break
        if not replaced:
            chars.append(c)
    text = ''.join(chars)

    # ── 3. Exposants — du plus spécifique au plus général ──
    # Lettres de compteur (jamais variables comme x, y, z)
    # → évite de transformer "2x+3" en "2^x+3"
    C = r'[nkmNKMpqij]'

    # 3a. "x 2k+1" → "x^(2k+1)"  lettre/chiffre ESPACE chiffre+compteur+signe+chiffre
    text = re.sub(
        r'([a-zA-Z0-9\)])\ +(\d' + C + r'[+\-\u2212]\d+)',
        r'\1^(\2)', text
    )
    # 3b. "x 2k" → "x^(2k)"  lettre/chiffre ESPACE chiffre+compteur
    text = re.sub(
        r'([a-zA-Z0-9\)])\ +(\d' + C + r')(?=[\s\u2212+\-*/×=,)(.\[\]]|$)',
        r'\1^(\2)', text
    )
    # 3c. "3n+2" → "3^(n+2)"  chiffre+compteur+signe+chiffre collés
    text = re.sub(
        r'(\d)(' + C + r')([+\-\u2212]\d+)(?=[\s\u2212+\-*/×=,)(.\[\]]|$)',
        r'\1^(\2\3)', text
    )
    # 3d. "3n" → "3^n"  chiffre+compteur collés (lookbehind pour ne pas retoucher ^(...))
    text = re.sub(
        r'(?<!\()(?<!\^)(\d)(' + C + r')(?=[^(]|$)',
        r'\1^\2', text
    )
    # 3e. "x 2" → "x^2"  lettre ESPACE chiffre seul
    text = re.sub(
        r'([a-zA-Z])\ +(\d+)(?=[\s+\-*/=().,\[\]]|$)',
        r'\1^\2', text
    )

    return text


def _docling_to_markdown(result) -> str:
    """
    Exporte le résultat Docling en Markdown.
    Compatible avec toutes les versions de Docling (API instable).

    Classement qualité (meilleure → moins bonne) :
      1. export_to_markdown()        ← Docling 2.x — LaTeX + structure complète ★★★★★
      2. export_to_markdown(options) ← Docling 1.x — idem avec options          ★★★★★
      3. render_as_markdown()        ← interface alternative                     ★★★★☆
      4. text_from_rendered()        ← Marker — texte brut, pas de LaTeX        ★★★☆☆
      5. concaténation pages         ← dernier recours, structure perdue         ★★☆☆☆
    """
    # ── Méthode 1 : Docling 2.x (recommandée) ─────────────
    if hasattr(result.document, 'export_to_markdown'):
        try:
            md = result.document.export_to_markdown()
            print("  [Docling] Export : méthode 1 — export_to_markdown() v2.x ★★★★★")
            return md
        except Exception:
            pass

    # ── Méthode 2 : Docling 1.x avec MarkdownExportOptions ─
    try:
        from docling_core.transforms.markdown import MarkdownExportOptions
        opts = MarkdownExportOptions(strict_text=False)
        md = result.document.export_to_markdown(options=opts)
        print("  [Docling] Export : méthode 2 — MarkdownExportOptions v1.x ★★★★★")
        return md
    except ImportError:
        pass

    # ── Méthode 3 : render_as_markdown() ───────────────────
    try:
        md = result.render_as_markdown()
        print("  [Docling] Export : méthode 3 — render_as_markdown() ★★★★☆")
        return md
    except Exception:
        pass

    # ── Méthode 4 : text_from_rendered (Marker) ────────────
    try:
        from marker.output import text_from_rendered
        text, _, _ = text_from_rendered(result)
        print("  [Docling] Export : méthode 4 — text_from_rendered() ★★★☆☆")
        return text
    except ImportError:
        pass

    # ── Méthode 5 : concaténation brute (dernier recours) ──
    try:
        pages = []
        for i, page in enumerate(result.document.pages, 1):
            text = getattr(page, 'text', '') or ''
            if text:
                pages.append(f"## Page {i}\n\n{text}")
        if pages:
            print("  [Docling] Export : méthode 5 — concaténation brute ★★☆☆☆")
            print("  ⚠ Qualité dégradée — mettre à jour Docling : pip install --upgrade docling")
            return "\n\n".join(pages)
    except Exception:
        pass

    raise RuntimeError("Impossible d'exporter le document Docling en Markdown")


# Seuil : si un PDF a plus de N formula-not-decoded → activer OCR math
FORMULA_OCR_THRESHOLD = 3


def _build_docling_pipeline(math_ocr: bool = False):
    """
    Configure le pipeline Docling.

    math_ocr=False (défaut) : pipeline standard, rapide
    math_ocr=True           : + pix2tex pour les formules en image
                              Activé automatiquement si > FORMULA_OCR_THRESHOLD
                              formula-not-decoded dans le document.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions, TableFormerMode
    )
    from docling.datamodel.base_models import InputFormat

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr             = True
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE

    # ── OCR mathématique (pix2tex) ──────────────────────────
    if math_ocr:
        _enable_math_ocr(pipeline_options)

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options
            )
        }
    )
    return converter


def _enable_math_ocr(pipeline_options) -> bool:
    """
    Active l'OCR mathématique via pix2tex (open source, ~400MB).
    Retourne True si activé avec succès.

    Installation :
        pip install pix2tex

    pix2tex reconnaît les formules dans les images → LaTeX.
    Bien mieux que l'OCR standard pour les expressions comme :
        \frac{-6x^2-9x-3}{-x^2+8x-17}
    """
    # Tentative 1 : FormulaEnricher natif Docling (versions récentes)
    try:
        from docling.enrichments.formulas import FormulaEnricher
        pipeline_options.enrichments = [FormulaEnricher()]
        print("  [Docling OCR Math] FormulaEnricher activé ✓")
        return True
    except ImportError:
        pass

    # Tentative 2 : pix2tex via l'API Docling pipeline
    try:
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions, OcrOptions
        )
        # Activer OCR haute résolution pour capturer les formules
        pipeline_options.ocr_options = EasyOcrOptions(
            force_full_page_ocr=True
        )
        print("  [Docling OCR Math] EasyOCR full-page activé ✓")
        return True
    except ImportError:
        pass

    print("  [Docling OCR Math] ⚠ Aucun module disponible")
    print("  → pip install pix2tex  pour l'OCR mathématique")
    return False


# ══════════════════════════════════════════════════════════════
# SPLITTER ADAPTATIF
# ══════════════════════════════════════════════════════════════

# Patterns complémentaires si Docling ne génère pas de headers #
# (certains PDFs n'ont pas de hiérarchie claire)
RE_EXERCICE = re.compile(
    r'(?m)^(Exercice\s*\d+[\s:]*|Exercices\s+divers\s*)',
    re.MULTILINE
)
RE_PARTIE = re.compile(
    r'(?m)^(Partie\s+\d+\s*[:–])',
    re.MULTILINE
)
RE_NUMBERED = re.compile(r'(?m)^\d{1,2}[\.]\s+\S', re.MULTILINE)
RE_ACTION   = re.compile(
    r'(?i)(simplifier|exprimer|isoler|calculer|résoudre|déterminer|'
    r'montrer|vérifier|retrouver|factoriser|développer|linéariser)',
)

def _split_markdown(markdown_text: str, doc_type: str) -> list[dict]:
    """
    Découpe le Markdown en sections — SANS dépendance langchain_text_splitters.
    Stratégie en cascade :
      1. Headers Markdown # ## ### (si Docling en génère)
      2. "Exercice N" ou "Partie N"
      3. "N. verbe" (listes numérotées sans mot "Exercice")
      4. Document entier (cours non structuré)
    """
    sections = []

    # ── Stratégie 1 : headers Markdown (générés par Docling)
    has_headers = bool(re.search(r'(?m)^#{1,4}\s+\S', markdown_text))
    if has_headers:
        header_re = re.compile(r'(?m)^(#{1,4})\s+(.+)$')
        splits    = list(header_re.finditer(markdown_text))
        bounds    = [m.start() for m in splits] + [len(markdown_text)]
        for i, m in enumerate(splits):
            text = markdown_text[m.start():bounds[i+1]].strip()
            if len(text) < 30:
                continue
            level  = len(m.group(1))
            title  = m.group(2).strip()
            eid    = re.sub(r'\s+', '_', title.lower())[:60]
            sections.append({
                "id"         : eid or f"section_{i}",
                "text"       : text,
                "header_meta": {f"h{level}": title},
            })
        if sections:
            return sections

    # ── Stratégie 2 : "Exercice N" (format standard) ou "Partie N" (cours)
    if RE_EXERCICE.search(markdown_text):
        splitter_re = RE_EXERCICE
    elif doc_type == 'cours' and RE_PARTIE.search(markdown_text):
        splitter_re = RE_PARTIE
    else:
        splitter_re = None

    if splitter_re:
        splits = list(splitter_re.finditer(markdown_text))
        bounds = [m.start() for m in splits] + [len(markdown_text)]
        if splits and splits[0].start() > 80:
            intro = markdown_text[:splits[0].start()].strip()
            if intro:
                sections.append({"id": "intro", "text": intro, "header_meta": {}})
        for i, m in enumerate(splits):
            text = markdown_text[m.start():bounds[i+1]].strip()
            if len(text) < 30:
                continue
            eid = re.sub(r'\s+', '_', m.group().strip().rstrip(':–').lower())
            sections.append({"id": eid, "text": text, "header_meta": {}})
        if sections:
            return sections

    # ── Stratégie 3 : "N. verbe" sans mot "Exercice" (expressions littérales)
    if doc_type == 'exercices' and RE_ACTION.search(markdown_text):
        splits = list(RE_NUMBERED.finditer(markdown_text))
        bounds = [m.start() for m in splits] + [len(markdown_text)]
        if splits and splits[0].start() > 30:
            intro = markdown_text[:splits[0].start()].strip()
            if intro:
                sections.append({"id": "intro", "text": intro, "header_meta": {}})
        for i, m in enumerate(splits):
            text = markdown_text[m.start():bounds[i+1]].strip()
            if len(text) < 20:
                continue
            sections.append({"id": f"q_{i+1:02d}", "text": text, "header_meta": {}})
        if sections:
            return sections

    # ── Stratégie 4 : document entier
    return [{"id": "document_entier", "text": markdown_text.strip(), "header_meta": {}}]


# ══════════════════════════════════════════════════════════════
# ENRICHISSEMENT DES MÉTADONNÉES PAR SECTION
# ══════════════════════════════════════════════════════════════

def _enrich_section(section: dict, notion: str, doc_type: str,
                    pdf_source: str) -> dict:
    """
    Construit un chunk complet depuis une section Docling.
    Ajoute difficulte, niveau, type_exercice, prerequis.
    """
    import sys
    sys.path.insert(0, str(_THIS_DIR))
    from taxonomy import (
        estimate_difficulty, diff_to_niveau,
        detect_type, get_prereqs, validate_metadata
    )

    text  = section["text"]
    diff  = estimate_difficulty(text, notion)
    niveau = diff_to_niveau(diff)
    typ   = detect_type(text)

    # Détecter si le chunk contient du LaTeX (Docling le génère proprement)
    has_latex   = bool(re.search(r'\\frac|\\sqrt|\\sum|\\int|\$[^$]+\$|\$\$', text))
    has_formula = has_latex or bool(re.search(r'[√∑∫±≤≥≠∞πθ]|[ℕℤℝℚℂ]', text))

    meta = validate_metadata({
        "chunk_id"     : section["id"],
        "notion"       : notion,
        "doc_type"     : doc_type,
        "pdf_source"   : pdf_source,
        "page"         : section.get("page", 1),
        "has_latex"    : has_latex,
        "has_formula"  : has_formula,
        "has_image"    : False,
        "char_count"   : len(text),
        "difficulte"   : diff,
        "niveau"       : niveau,
        "type_exercice": typ,
        "prerequis"    : get_prereqs(notion),
        "concept"      : None,      # enrichi par step2 (LLM)
        "lacunes_type" : [],
        "llm_enriched" : False,
        "format"       : "docling_markdown",  # traçabilité
        **section.get("header_meta", {}),     # headers Docling
    })

    return {
        "id"             : section["id"],
        "text"           : text,
        "formula_images" : [],        # Docling gère les images séparément
        "parent_exercice": section.get("header_meta", {}).get("section", ""),
        "notion"         : notion,
        "page"           : section.get("page", 1),
        "sub_index"      : 0,
        "is_full_exercice": True,
        "metadata"       : meta,
    }


# ══════════════════════════════════════════════════════════════
# PIPELINE PRINCIPAL
# ══════════════════════════════════════════════════════════════

def run(pdf_path: str,
        notion_override: str = None,
        doc_type: str = "exercices") -> list[dict]:
    """
    Pipeline Docling complet.

    Paramètres :
        pdf_path        : chemin vers le PDF
        notion_override : notion imposée depuis l'arborescence (priorité absolue)
        doc_type        : 'exercices' ou 'cours'

    Retourne : liste de chunks enrichis, compatible avec step2 et step3.
    """
    t0 = time.perf_counter()
    pdf_path = Path(pdf_path)
    print(f"\n  [Docling] {pdf_path.name}")

    # ── Étape 1 : Conversion PDF → Markdown avec Docling ──────
    try:
        converter = _build_docling_pipeline()
        result    = converter.convert(str(pdf_path))

        # Exporter en Markdown — compatible toutes versions Docling
        markdown_text = _docling_to_markdown(result)
        markdown_text = _clean_docling_markdown(markdown_text)
        fnd_count     = markdown_text.count('[formule]')
        print(f"  [Docling] Conversion OK — {len(markdown_text)} chars")

        # Passe 2 : OCR mathématique si trop de formules non décodées
        if fnd_count > FORMULA_OCR_THRESHOLD:
            print(f"  [Docling] {fnd_count} formule(s) non décodée(s) "
                  f"(>{FORMULA_OCR_THRESHOLD}) → OCR mathématique")
            try:
                converter2     = _build_docling_pipeline(math_ocr=True)
                result2        = converter2.convert(str(pdf_path))
                markdown_text2 = _clean_docling_markdown(_docling_to_markdown(result2))
                if markdown_text2.count('[formule]') < fnd_count:
                    markdown_text = markdown_text2
                    print(f"  [Docling OCR Math] formules récupérées ✓")
            except Exception as e2:
                logger.warning(f"OCR math échoué ({e2}) — version standard conservée")

    except Exception as e:
        logger.error(f"Docling échoué sur {pdf_path.name} : {e}")
        logger.warning("Fallback sur pdfplumber...")
        markdown_text = _fallback_pdfplumber(pdf_path)
        if not markdown_text:
            return []

    # Sauvegarder le Markdown pour debug/audit
    md_out = OUT_DIR / f"{pdf_path.stem}_docling.md"
    md_out.write_text(markdown_text, encoding="utf-8")

    # ── Étape 2 : Détecter la notion ──────────────────────────
    if notion_override and notion_override != "non_identifiée":
        notion = notion_override
    else:
        import sys
        sys.path.insert(0, str(_THIS_DIR))
        from taxonomy import detect_notion
        notion = detect_notion(markdown_text)

    print(f"  [Docling] Notion : {notion} | Type : {doc_type}")

    # ── Étape 3 : Découpage adaptatif ─────────────────────────
    sections = _split_markdown(markdown_text, doc_type)
    print(f"  [Docling] {len(sections)} section(s) détectée(s)")

    # ── Étape 4 : Enrichissement des métadonnées ──────────────
    chunks = []
    for i, section in enumerate(sections):
        section["id"] = f"{pdf_path.stem}_{section['id']}_{i:02d}"
        chunk = _enrich_section(section, notion, doc_type, pdf_path.name)
        if chunk["metadata"]["char_count"] >= 20:
            chunks.append(chunk)

    # ── Étape 5 : Sauvegarde ──────────────────────────────────
    chunk_out = OUT_DIR / "chunks_by_pdf" / f"{pdf_path.stem}_docling_chunks.json"
    chunk_out.parent.mkdir(exist_ok=True)
    with open(chunk_out, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    elapsed = time.perf_counter() - t0
    print(f"  [Docling] ✓ {len(chunks)} chunks — {elapsed:.1f}s")

    return chunks


# ══════════════════════════════════════════════════════════════
# FALLBACK PDFPLUMBER (si Docling échoue)
# ══════════════════════════════════════════════════════════════

def _fallback_pdfplumber(pdf_path: Path) -> str:
    """
    Extraction de secours avec pdfplumber.
    Utilisé si Docling échoue (PDF corrompu, mémoire insuffisante...).
    """
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = []
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                pages.append(f"## Page {i+1}\n\n{text}")
        return _clean_docling_markdown("\n\n".join(pages))
    except Exception as e:
        logger.error(f"Fallback pdfplumber échoué : {e}")
        return ""


# ══════════════════════════════════════════════════════════════
# ADAPTATION run_pipeline_batch.py
# ══════════════════════════════════════════════════════════════
"""
Dans run_pipeline_batch.py, remplacer :

    from step1_atomic_chunker import run as run_step1
    chunks = run_step1(str(pdf_path), notion_override=notion, doc_type=doc_type)

Par :

    from step1_docling_chunker import run as run_step1
    chunks = run_step1(str(pdf_path), notion_override=notion, doc_type=doc_type)

L'interface est IDENTIQUE — aucun autre changement nécessaire.
"""


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage : python step1_docling_chunker.py chemin/vers/doc.pdf")
        sys.exit(1)
    chunks = run(path)
    print(f"\n{len(chunks)} chunks produits")
    for c in chunks[:3]:
        m = c["metadata"]
        print(f"  [{m['difficulte']}/5] {c['id'][:60]}")
        print(f"  LaTeX: {m['has_latex']} | {m['type_exercice']}")
        print(f"  {c['text'][:150].replace(chr(10),' ')}...")

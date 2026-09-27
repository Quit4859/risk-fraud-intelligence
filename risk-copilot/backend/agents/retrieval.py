"""Document parsing and clause-level retrieval.

Turns the markdown policy library on disk into citable clause chunks, then
serves BM25-ranked retrieval over them. Every regulatory sentence the copilot
emits is anchored to a ``clause_ref`` returned from here - that anchor is what
makes the output audit-ready rather than merely plausible.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
FM_KEY_RE = re.compile(r"^([a-z_]+):\s*(.*)$", re.M)
HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$")
# Clause ids look like CTR-1.2.2, STR-2.1.1, SCR-1.1.2, NBFC-3.1.1, AUTO-3.1.2
CLAUSE_RE = re.compile(r"^([A-Z][A-Z0-9]{1,6}-\d+(?:\.\d+)*)\s+(.*)$")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]{1,}")

# Words that appear in almost every clause and therefore carry no signal.
STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "shall", "must", "are",
    "was", "were", "has", "have", "been", "which", "when", "where", "into", "any",
    "its", "not", "than", "such", "each", "may", "can", "but", "also", "their",
    "there", "these", "those", "other", "than", "them", "then", "more", "most",
    "over", "under", "after", "before", "all", "any", "one", "two", "per", "via",
}


def tokenize(text: str) -> List[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 2]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_front_matter(text: str) -> Dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    for key, value in FM_KEY_RE.findall(m.group(1)):
        out[key] = value.strip().strip('"')
    return out


def parse_markdown_document(path: str) -> Dict[str, Any]:
    """Split a policy markdown file into clause chunks with a document header."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    meta = parse_front_matter(text)
    body = FRONTMATTER_RE.sub("", text)
    doc_id = meta.get("doc_id", os.path.splitext(os.path.basename(path))[0])

    title = meta.get("title", "")
    if not title:
        h1 = re.search(r"^#\s+(.*)$", body, re.M)
        title = h1.group(1).strip() if h1 else doc_id

    chunks: List[Dict[str, Any]] = []
    section = ""
    buffer: List[str] = []
    clause_ref: Optional[str] = None
    clause_title = ""
    seq = 0

    def flush():
        nonlocal buffer, clause_ref, clause_title, seq
        if clause_ref and buffer:
            body_text = re.sub(r"\s+", " ", " ".join(buffer)).strip()
            title_text = re.sub(r"\s+", " ", clause_title).strip()
            if body_text:
                seq += 1
                chunks.append({
                    "chunk_id": f"{doc_id}:{clause_ref}",
                    "doc_id": doc_id,
                    "framework": meta.get("framework", "Internal"),
                    "title": title,
                    "jurisdiction": meta.get("jurisdiction", "International"),
                    "section": section,
                    "clause_ref": clause_ref,
                    "title_text": title_text,
                    "body": body_text,
                    "effective_date": meta.get("effective_date", ""),
                    "token_count": len(tokenize(f"{title_text} {body_text}")),
                })
        buffer = []
        clause_ref = None
        clause_title = ""

    for line in body.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            flush()
            section = heading.group(2).strip()
            continue
        clause = CLAUSE_RE.match(line)
        if clause:
            flush()
            clause_ref = clause.group(1)
            clause_title = clause.group(2)
            continue
        if clause_ref:
            buffer.append(line)

    flush()

    return {
        "doc_id": doc_id,
        "title": title,
        "framework": meta.get("framework", "Internal"),
        "jurisdiction": meta.get("jurisdiction", "International"),
        "version": meta.get("version", ""),
        "effective_date": meta.get("effective_date", ""),
        "regulatory_body": meta.get("regulatory_body", ""),
        "source": meta.get("source", ""),
        "chunks": chunks,
        "path": path,
    }


def parse_corpus(corpus_dir: str) -> List[Dict[str, Any]]:
    docs = []
    for fname in sorted(os.listdir(corpus_dir)):
        if fname.endswith(".md"):
            docs.append(parse_markdown_document(os.path.join(corpus_dir, fname)))
    return docs


# --------------------------------------------------------------------------
# BM25 retrieval
# --------------------------------------------------------------------------


class BM25Index:
    """Compact BM25 over clause chunks. No external dependency, deterministic."""

    def __init__(self, chunks: Sequence[Dict[str, Any]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.chunks: List[Dict[str, Any]] = []
        self.docs: List[Counter] = []
        self.df: Dict[str, int] = defaultdict(int)
        self.doc_freq_len: List[int] = []
        self._build(chunks)

    def _build(self, chunks: Sequence[Dict[str, Any]]) -> None:
        for chunk in chunks:
            tokens = tokenize(f"{chunk['title_text']} {chunk['body']}")
            if not tokens:
                continue
            tf = Counter(tokens)
            self.chunks.append({**chunk, "_tokens": tokens})
            self.docs.append(tf)
            self.doc_freq_len.append(len(tokens))
            for term in tf:
                self.df[term] += 1
        self.avgdl = (sum(self.doc_freq_len) / len(self.doc_freq_len)) if self.docs else 1.0
        self.n = len(self.docs)

    def search(self, query: str, top_k: int = 5, frameworks: Optional[Iterable[str]] = None,
               min_score: float = 0.0) -> List[Dict[str, Any]]:
        q_terms = tokenize(query)
        if not q_terms or not self.n:
            return []
        fset = {f.upper() for f in frameworks} if frameworks else None

        results = []
        for i, chunk in enumerate(self.chunks):
            if fset and chunk["framework"].upper() not in fset:
                continue
            score = 0.0
            tf = self.docs[i]
            dl = self.doc_freq_len[i]
            for term in q_terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                n_q = self.df[term]
                idf = math.log(1 + (self.n - n_q + 0.5) / (n_q + 0.5))
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += idf * (f * (self.k1 + 1)) / denom
            # A clause-id or framework token in the query is a hard boost so that
            # "CTR-1.2.2" style questions resolve to the exact clause.
            if score > min_score:
                result = {k: v for k, v in chunk.items() if k != "_tokens"}
                result["score"] = round(score, 4)
                result["citation"] = f"{chunk['doc_id']} s.{chunk['section']} {chunk['clause_ref']}"
                results.append(result)
        results.sort(key=lambda r: (-r["score"], r["clause_ref"]))
        return results[:top_k]

    def by_clause_prefix(self, prefix: str) -> List[Dict[str, Any]]:
        """Exact clause lookup, used to pin citations instead of guessing them."""
        out = []
        for chunk in self.chunks:
            if chunk["clause_ref"].upper().startswith(prefix.upper()):
                result = {k: v for k, v in chunk.items() if k != "_tokens"}
                result["score"] = 1.0
                result["citation"] = f"{chunk['doc_id']} s.{chunk['section']} {chunk['clause_ref']}"
                out.append(result)
        return out


class RetrievalService:
    """Corpus parsing + index lifecycle, with an on-disk cache of chunks."""

    def __init__(self, warehouse=None, corpus_dir: Optional[str] = None, config: Optional[Dict] = None):
        from backend.config import load_config
        self.config = config or load_config()
        self.warehouse = warehouse
        self.corpus_dir = corpus_dir or os.path.join(
            self.config["_root"], self.config["paths"]["corpus_dir"])
        self.docs: List[Dict[str, Any]] = []
        self.index: Optional[BM25Index] = None
        self.last_error: Optional[str] = None

    def load(self) -> "RetrievalService":
        try:
            self.docs = parse_corpus(self.corpus_dir)
            chunks = [c for d in self.docs for c in d["chunks"]]
            rcfg = self.config["retrieval"]
            self.index = BM25Index(chunks, k1=rcfg["bm25_k1"], b=rcfg["bm25_b"])
            self.last_error = None
        except Exception as exc:  # guardrail: never break the copilot on the corpus
            self.docs, self.index = [], None
            self.last_error = str(exc)
        if self.warehouse is not None and self.index is not None:
            self._persist()
        return self

    def _persist(self) -> None:
        wh = self.warehouse
        wh.execute("DELETE FROM document_chunks")
        for doc in self.docs:
            for c in doc["chunks"]:
                wh.execute(
                    "INSERT OR REPLACE INTO document_chunks (chunk_id, doc_id, framework, title,"
                    " jurisdiction, section, clause_ref, title_text, body, effective_date, token_count)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (c["chunk_id"], c["doc_id"], c["framework"], c["title"], c["jurisdiction"],
                     c["section"], c["clause_ref"], c["title_text"], c["body"],
                     c["effective_date"], c["token_count"]))
        wh.execute("DELETE FROM policies")
        import json
        pol_path = os.path.join(self.config["_root"], "data", "gold", "policies.json")
        if os.path.exists(pol_path):
            with open(pol_path, "r", encoding="utf-8") as fh:
                policies = json.load(fh)
            for p in policies:
                wh.execute(
                    "INSERT OR REPLACE INTO policies (policy_id, framework, name, jurisdiction,"
                    " regulatory_body, description, applies_to, risk_levels, effective_date,"
                    " version, key_thresholds, binding_rule_code, obligation, source_doc_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["policy_id"], p["framework"], p["name"], p["jurisdiction"],
                     p["regulatory_body"], p["description"],
                     json.dumps(p["applies_to"]), json.dumps(p["risk_levels"]),
                     p["effective_date"], p["version"], json.dumps(p["key_thresholds"]),
                     p.get("binding_rule_code"), p["obligation"], p["source_doc_id"]))

    @property
    def clause_count(self) -> int:
        return len(self.index.chunks) if self.index else 0

    def search(self, query: str, top_k: Optional[int] = None,
               frameworks: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        if not self.index:
            return []
        rcfg = self.config["retrieval"]
        return self.index.search(
            query,
            top_k=top_k or rcfg["top_k_chunks"],
            frameworks=frameworks,
            min_score=rcfg["min_chunk_score"],
        )

    def clauses_for_rules(self, rule_codes: Iterable[str]) -> List[Dict[str, Any]]:
        """Pin citations from rule code -> policy -> source document -> clause."""
        if self.warehouse is None:
            return []
        out: List[Dict[str, Any]] = []
        seen = set()
        codes = [c for c in rule_codes if c]
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        rows = self.warehouse.query(
            f"SELECT * FROM policies WHERE binding_rule_code IN ({placeholders})", codes)
        for row in rows:
            prefix = _rule_to_clause_prefix(row["binding_rule_code"], row["source_doc_id"])
            for clause in self.index.by_clause_prefix(prefix) if self.index else []:
                if clause["chunk_id"] in seen:
                    continue
                seen.add(clause["chunk_id"])
                clause["policy_id"] = row["policy_id"]
                clause["obligation"] = row["obligation"]
                out.append(clause)
        return out

    def get_clause(self, clause_ref: str) -> Optional[Dict[str, Any]]:
        if not self.index:
            return None
        hits = self.index.by_clause_prefix(clause_ref)
        return hits[0] if hits else None


def _rule_to_clause_prefix(rule_code: Optional[str], doc_id: str) -> str:
    """Map a detection rule code to the clause family that governs it."""
    if not rule_code:
        return "NO-MATCH-"
    return {
        "STRUCTURING": "STR-",
        "VELOCITY": "MON-",
        "GEOGRAPHIC": "GEO-",
        "ACCOUNT_TAKEOVER": "GEO-2.2",
        "MULE_NETWORK": "STR-2",
        "TRADE_BASED": "TBM-",
        "COMPOSITE": "ESC-",
        "CREDIT": "ECL-",
        "KYC": "CDD-",
    }.get(rule_code.upper(), "SAR-")

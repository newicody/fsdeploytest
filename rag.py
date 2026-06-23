#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
rag.py — moteur RAG multi-domaines au-dessus du registre de pipelines.

- decoupe les documents en chunks (par section : titre + paragraphe)
- indexe PAR DOMAINE : un DomainIndex = vecteurs + metadonnees (chunks)
- recherche : embed(question) -> cosinus brut (numpy) -> top_k
- affine : rerank pipeline optionnel -> top_n
- build_context : assemble les meilleurs chunks + leurs sources, pret a
  injecter dans le LLM (comme le mode Projet, mais calcul 100% local)

Extensible : le modele d'embedding/rerank vient du registre (ov_pipelines),
donc en changer = changer la config, pas le code. De nouveaux pipelines
(vlm, whisper...) s'ajoutent par simple sous-classe (cf. ov_pipelines).

Le calcul reste cote machine (CPU/OpenVINO) ; une UI web ne ferait que la
selection des sources et l'affichage, pas l'inference.
"""
import json
import os
import re
from pathlib import Path

import ov_pipelines


def _autoboot_home():
    """Home de l'utilisateur de session (donnees durables sur data_pool/home).
    Resout le home de l'utilisateur COURANT (multi-utilisateurs supporte) :
    AUTOBOOT_HOME (exporte par session_launch) prioritaire ; sinon le home du
    compte effectif (pwd) ; sinon $HOME si non-root. Pas de compte code en dur."""
    h = os.environ.get("AUTOBOOT_HOME")
    if h:
        return h
    try:
        import pwd
        pw = pwd.getpwuid(os.getuid())
        if pw.pw_dir and pw.pw_dir not in ("/", "/root", ""):
            return pw.pw_dir
    except (KeyError, ImportError):
        pass
    h = os.path.expanduser("~")
    if h and h not in ("/root", "/", ""):
        return h
    # dernier recours : home derive du nom d'utilisateur effectif sous /home
    try:
        import pwd
        return os.path.join("/home", pwd.getpwuid(os.getuid()).pw_name)
    except Exception:
        return os.path.expanduser("~") or "/tmp"


def _autoboot_dir(kind):
    return os.path.join(_autoboot_home(), ".autoboot", kind)


def _model(name):
    """Resout un modele sous MODELS_DIR (data_pool/modeles, defaut /var/lib/
    models). Accepte un chemin absolu, 'models/foo-ov' (legacy) ou 'foo-ov'."""
    if os.path.isabs(name):
        return name
    return os.path.join(os.environ.get("MODELS_DIR", "/var/lib/models"),
                        os.path.basename(name))

try:
    import numpy as np
except ImportError:
    np = None


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #
def chunk_text(text, max_chars=1200):
    """Decoupe par section semantique : on coupe sur les lignes vides et les
    titres (markdown '#', ou ligne courte suivie d'une longue). Chaque chunk
    garde en prefixe le dernier titre vu (contexte pour le rerank).
    Genere des tuples (heading, body)."""
    heading = ""
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        first = block.splitlines()[0].strip()
        is_md_head = first.startswith("#")
        is_short_head = (len(first) <= 80 and len(block.splitlines()) == 1
                         and first.endswith(":"))
        if is_md_head or is_short_head:
            heading = first.lstrip("# ").rstrip(":").strip()
            # un titre seul ne fait pas un chunk ; on l'attache au suivant
            if len(block.splitlines()) == 1:
                continue
        # tronconner les blocs trop longs
        for piece in _split_long(block, max_chars):
            yield heading, piece


def _split_long(s, max_chars):
    if len(s) <= max_chars:
        yield s
        return
    # coupe sur les phrases
    cur = ""
    for sent in re.split(r"(?<=[.!?])\s+", s):
        if len(cur) + len(sent) + 1 > max_chars and cur:
            yield cur.strip()
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        yield cur


# --------------------------------------------------------------------------- #
# chunk
# --------------------------------------------------------------------------- #
class Chunk:
    __slots__ = ("id", "text", "source", "domain", "heading")

    def __init__(self, id, text, source, domain, heading=""):
        self.id = id
        self.text = text
        self.source = source
        self.domain = domain
        self.heading = heading

    @property
    def embed_text(self):
        """Texte effectivement encode : titre + corps (le titre aide le match)."""
        return f"{self.heading}\n{self.text}" if self.heading else self.text

    def to_dict(self):
        return {"id": self.id, "text": self.text, "source": self.source,
                "domain": self.domain, "heading": self.heading}

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["text"], d["source"], d["domain"],
                   d.get("heading", ""))

    def __repr__(self):
        return f"<Chunk {self.id} [{self.domain}] {self.source}>"


# --------------------------------------------------------------------------- #
# index d'un domaine
# --------------------------------------------------------------------------- #
class DomainIndex:
    def __init__(self, domain):
        self.domain = domain
        self.chunks = []
        self._vecs = []          # liste de listes (ou matrice np apres _seal)
        self._matrix = None      # cache np (n, d), lignes normalisees

    def __len__(self):
        return len(self.chunks)

    def __iter__(self):
        return iter(self.chunks)

    def __getitem__(self, i):
        try:
            return self.chunks[i]
        except IndexError:
            raise IndexError(
                f"chunk #{i} hors limites (domaine '{self.domain}' "
                f"contient {len(self.chunks)} chunk(s))") from None

    def add(self, chunks, vectors):
        for c, v in zip(chunks, vectors):
            self.chunks.append(c)
            self._vecs.append(list(v))
        self._matrix = None      # invalide le cache

    def _seal(self):
        if np is None:
            return None
        if self._matrix is None and self._vecs:
            m = np.asarray(self._vecs, dtype="float32")
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = m / norms
        return self._matrix

    def search(self, qvec, top_k=20):
        """Retourne [(Chunk, score_cosinus)] tries decroissant."""
        if not self.chunks:
            return []
        if np is not None:
            m = self._seal()
            q = np.asarray(qvec, dtype="float32")
            q = q / (np.linalg.norm(q) or 1.0)
            sims = m @ q
            order = sims.argsort()[::-1][:top_k]
            return [(self.chunks[i], float(sims[i])) for i in order]
        # repli pur-python
        q = list(qvec)
        qn = math_sqrt(sum(x * x for x in q)) or 1.0
        scored = []
        for c, v in zip(self.chunks, self._vecs):
            vn = math_sqrt(sum(x * x for x in v)) or 1.0
            dot = sum(a * b for a, b in zip(q, v))
            scored.append((c, dot / (qn * vn)))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k]

    # --- persistance : chunks.jsonl + vectors.npy (ou vectors.json) --------
    def save(self, base_dir):
        d = Path(base_dir) / self.domain
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "chunks.jsonl", "w") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_dict()) + "\n")
        if np is not None:
            np.save(d / "vectors.npy", np.asarray(self._vecs, dtype="float32"))
        else:
            (d / "vectors.json").write_text(json.dumps(self._vecs))
        return str(d)

    @classmethod
    def load(cls, base_dir, domain):
        d = Path(base_dir) / domain
        idx = cls(domain)
        cf = d / "chunks.jsonl"
        if not cf.exists():
            return idx
        idx.chunks = [Chunk.from_dict(json.loads(ln))
                      for ln in cf.read_text().splitlines() if ln.strip()]
        if (d / "vectors.npy").exists() and np is not None:
            idx._vecs = [list(row) for row in np.load(d / "vectors.npy")]
        elif (d / "vectors.json").exists():
            idx._vecs = json.loads((d / "vectors.json").read_text())
        return idx


def math_sqrt(x):
    import math
    return math.sqrt(x)


# --------------------------------------------------------------------------- #
# moteur RAG
# --------------------------------------------------------------------------- #
class RagEngine:
    """Gere plusieurs DomainIndex + l'embedding/rerank via le registre.

    embedder/reranker : instances de Pipeline (kind embedding/rerank), ou None
    -> crees a la demande a partir des chemins de modele fournis.
    """

    def __init__(self, root=None, embedder=None, reranker=None,
                 embed_model=None, rerank_model=None):
        self.root = root or _autoboot_dir("rag")
        self._domains = {}       # domaine -> DomainIndex
        self._embedder = embedder
        self._reranker = reranker
        self._embed_model = embed_model or _model("qwen3-embedding-0.6b-ov")
        self._rerank_model = rerank_model or _model("qwen3-reranker-0.6b-ov")

    # --- acces pipelines (lazy) -------------------------------------------
    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = ov_pipelines.create("embedding", self._embed_model)
        return self._embedder

    @property
    def reranker(self):
        if self._reranker is None:
            self._reranker = ov_pipelines.create("rerank", self._rerank_model)
        return self._reranker

    # --- acces domaines avec message --------------------------------------
    def __getitem__(self, domain):
        try:
            return self._domains[domain]
        except KeyError:
            raise KeyError(
                f"domaine '{domain}' non charge ; disponibles : "
                f"{sorted(self._domains)} (ingest ou load_domain d'abord)"
            ) from None

    def __contains__(self, domain):
        return domain in self._domains

    def __iter__(self):
        for name in sorted(self._domains):
            yield name, self._domains[name]

    def domains(self):
        return sorted(self._domains)

    def _index(self, domain):
        if domain not in self._domains:
            self._domains[domain] = DomainIndex(domain)
        return self._domains[domain]

    # --- ingestion (genere la progression) --------------------------------
    def ingest(self, domain, text, source, max_chars=1200):
        """Decoupe, encode et ajoute au domaine. Genere (i, total, Chunk)
        pour permettre un affichage de progression cote appelant."""
        pieces = list(chunk_text(text, max_chars))
        total = len(pieces)
        idx = self._index(domain)
        base = len(idx)
        chunks = [Chunk(f"{domain}:{source}:{base + i}", body, source, domain,
                        heading)
                  for i, (heading, body) in enumerate(pieces)]
        if not chunks:
            return
        vectors = self.embedder.embed([c.embed_text for c in chunks])
        idx.add(chunks, vectors)
        for i, c in enumerate(chunks):
            yield (i + 1, total, c)

    def ingest_file(self, domain, path):
        text = Path(path).read_text(errors="replace")
        yield from self.ingest(domain, text, source=os.path.basename(path))

    # --- recherche ---------------------------------------------------------
    def query(self, question, domains=None, top_k=20, top_n=5, rerank=True):
        """Cherche dans les domaines donnes (ou tous), fusionne, puis rerank.
        Retourne [(Chunk, score)] de longueur <= top_n."""
        targets = domains or self.domains()
        qvec = self.embedder.embed_query(question)
        pooled = []
        for dom in targets:
            if dom in self._domains:
                pooled.extend(self._domains[dom].search(qvec, top_k))
        pooled.sort(key=lambda t: t[1], reverse=True)
        pooled = pooled[:top_k]
        if not pooled:
            return []
        if rerank:
            docs = [c.embed_text for c, _ in pooled]
            scored = self.reranker.rerank(question, docs)
            # re-mapper sur les chunks (rerank conserve l'ordre d'entree puis trie)
            by_text = {}
            for c, _ in pooled:
                by_text.setdefault(c.embed_text, c)
            out = [(by_text[d], s) for d, s in scored if d in by_text]
            return out[:top_n]
        return pooled[:top_n]

    def build_context(self, question, domains=None, top_n=5, **kw):
        """Assemble un bloc de contexte cite, pret pour le prompt LLM."""
        hits = self.query(question, domains=domains, top_n=top_n, **kw)
        if not hits:
            return "", []
        blocks, sources = [], []
        for rank, (c, score) in enumerate(hits, 1):
            tag = f"[{rank}] {c.source}" + (f" — {c.heading}" if c.heading else "")
            blocks.append(f"{tag}\n{c.text}")
            sources.append({"rank": rank, "source": c.source,
                            "heading": c.heading, "domain": c.domain,
                            "score": round(float(score), 4)})
        ctx = "\n\n".join(blocks)
        return ctx, sources

    # --- persistance -------------------------------------------------------
    def save(self):
        for _, idx in self:
            idx.save(self.root)
        return self.root

    def load_domain(self, domain):
        self._domains[domain] = DomainIndex.load(self.root, domain)
        return self._domains[domain]

    def load_all(self):
        base = Path(self.root)
        if base.is_dir():
            for d in base.iterdir():
                if (d / "chunks.jsonl").exists():
                    self.load_domain(d.name)
        return self.domains()


# --------------------------------------------------------------------------- #
# CLI minimale
# --------------------------------------------------------------------------- #
def main():
    import argparse
    ap = argparse.ArgumentParser(description="RAG multi-domaines (local)")
    ap.add_argument("--root", default=None,
                    help="racine des index (defaut: ~/.autoboot/rag ; chaque "
                         "sous-dossier = un domaine/projet : kernel, python3, ...)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest")
    pi.add_argument("domain")
    pi.add_argument("path")
    pq = sub.add_parser("query")
    pq.add_argument("question")
    pq.add_argument("--domain", action="append", default=None)
    sub.add_parser("list")
    a = ap.parse_args()

    eng = RagEngine(root=a.root)
    eng.load_all()

    if a.cmd == "ingest":
        n = 0
        for i, total, c in eng.ingest_file(a.domain, a.path):
            n = total
            print(f"\r  {i}/{total} {c.source}", end="", flush=True)
        eng.save()
        print(f"\n{n} chunk(s) indexe(s) dans '{a.domain}' -> {eng.root}")
    elif a.cmd == "query":
        ctx, sources = eng.build_context(a.question, domains=a.domain)
        if not sources:
            print("aucun resultat.")
            return
        print("=== sources ===")
        for s in sources:
            print(f"  [{s['rank']}] {s['source']} ({s['domain']}, "
                  f"score={s['score']})")
        print("\n=== contexte ===\n" + ctx)
    elif a.cmd == "list":
        for name, idx in eng:
            print(f"  {name}: {len(idx)} chunk(s)")


if __name__ == "__main__":
    main()

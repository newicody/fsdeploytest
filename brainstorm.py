#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
brainstorm.py — flux d'idees : push -> marquage -> dispatch local -> board.

Format pivot = FICHE IDEE JSON (texte + metadonnees), pas de binaire. C'est du
texte que l'inference consomme (embedding + prompt) et que git versionne /
diff / cite. Le calcul reste 100% local (OpenVINO via ov_pipelines) ; GitHub
n'est qu'orchestration et tableau de validation.

Cinematique :
  entree (fichier pushe | observation de boot | note manuelle)
    -> normalize_to_idea()  : fiche JSON standard
    -> route()              : routeur local choisit le(s) domaine(s) RAG
    -> develop()            : LLM generaliste + contexte RAG des domaines
    -> fiche enrichie (corps developpe + sources citees + score)
    -> board (label a-valider / en-dev / drop) : gere ailleurs (git/Projects)

Le premier marquage externe (Copilot/GitHub Action) est OPTIONNEL et
bypassable : sans score fourni, le routeur local s'en charge. Travailler
uniquement en inference locale est le mode par defaut.

Reutilise ov_pipelines (registre) et rag (contexte). Stdlib + ces deux modules.
"""
import json
import os
import time
import uuid
from pathlib import Path

import ov_pipelines


def _autoboot_home():
    """Home de session (durable, data_pool/home). AUTOBOOT_HOME prioritaire ;
    sinon le home du compte effectif (pwd) ; sinon $HOME. Multi-utilisateurs."""
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

# statuts du board (cycle de vie). prod = declencheur d'action (compilation).
S_IDEA, S_WIP, S_DEV, S_PROD, S_DROP = "idea", "wip", "dev", "prod", "drop"
VALID_STATUS = (S_IDEA, S_WIP, S_DEV, S_PROD, S_DROP)

# origines d'une idee
O_PUSH, O_BOOT, O_MANUAL = "push", "boot", "manual"


# --------------------------------------------------------------------------- #
# fiche idee
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# candidat (couche EXPLORATION : volatile, regenere par l'inference)
# --------------------------------------------------------------------------- #
class Candidate:
    """Une solution candidate issue de l'inference. Independante de l'etat et
    de l'acte : l'inference peut en ajouter/regenerer librement."""
    __slots__ = ("id", "summary", "body", "sources", "score", "created")

    def __init__(self, summary, body, sources=None, score=0.0, id=None):
        self.id = id or uuid.uuid4().hex[:8]
        self.summary = summary
        self.body = body
        self.sources = sources or []
        self.score = float(score)
        self.created = int(time.time())

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        c = cls(d["summary"], d["body"], d.get("sources"), d.get("score", 0.0),
                id=d.get("id"))
        c.created = d.get("created", c.created)
        return c

    def __repr__(self):
        return f"<Candidate {self.id} score={self.score:.2f} '{self.summary[:36]}'>"


# --------------------------------------------------------------------------- #
# fiche idee : 3 couches independantes
#   - candidates : EXPLORATION (inference, volatile, navigable/diffable)
#   - acted      : ACTE (proprietes validees par l'humain, stables -> actions)
#   - status     : ETAT (cycle de vie ; prod = declencheur)
# acted_rev/applied_rev : ce qui est acte mais pas encore execute = "en attente"
# --------------------------------------------------------------------------- #
class Idea:
    """Fiche idee serialisable. Acces controle avec messages explicites."""
    __slots__ = ("id", "title", "body", "origin", "source", "domains",
                 "status", "score", "created", "updated", "history",
                 "candidates", "acted", "chosen", "acted_rev", "applied_rev",
                 "issue")

    def __init__(self, title, body, origin=O_MANUAL, source="", id=None):
        self.id = id or uuid.uuid4().hex[:12]
        self.title = title
        self.body = body
        self.origin = origin
        self.source = source
        self.domains = []
        self.status = S_IDEA
        self.score = 0.0
        self.created = int(time.time())
        self.updated = self.created
        self.history = []        # liste d'evenements {ts, note}
        # 3 couches
        self.candidates = []     # liste de Candidate (exploration)
        self.acted = {}          # proprietes actees (cle -> valeur), stables
        self.chosen = None       # id du candidat retenu (ou None)
        self.acted_rev = 0       # incremente a chaque act/unact
        self.applied_rev = 0     # = acted_rev quand le serveur a execute
        self.issue = None        # numero d'Issue GitHub (lien board)

    # --- etat (independant des 2 autres couches) --------------------------
    def set_status(self, value):
        if value not in VALID_STATUS:
            raise ValueError(
                f"statut invalide '{value}' ; attendu un de {VALID_STATUS}")
        self.status = value
        self.touch(f"status -> {value}")

    def touch(self, note=""):
        self.updated = int(time.time())
        if note:
            self.history.append({"ts": self.updated, "note": note})

    # --- couche EXPLORATION : candidats ------------------------------------
    def add_candidate(self, summary, body, sources=None, score=0.0):
        c = Candidate(summary, body, sources, score)
        self.candidates.append(c)
        self.touch(f"candidat + {c.id}")
        return c

    def candidate(self, cid):
        for c in self.candidates:
            if c.id == cid:
                return c
        raise KeyError(
            f"candidat '{cid}' inconnu ; disponibles : "
            f"{[c.id for c in self.candidates]}")

    def choose(self, cid):
        self.candidate(cid)             # valide l'existence (message si absent)
        self.chosen = cid
        self.touch(f"choix candidat {cid}")
        return self

    def diff_candidates(self, a_id, b_id):
        """Diff unifie texte entre deux candidats (navigation des solutions)."""
        import difflib
        a, b = self.candidate(a_id), self.candidate(b_id)
        return "\n".join(difflib.unified_diff(
            a.body.splitlines(), b.body.splitlines(),
            fromfile=f"candidat:{a_id}", tofile=f"candidat:{b_id}", lineterm=""))

    # --- couche ACTE : proprietes validees (independante de l'etat) --------
    def act(self, key, value):
        """Acte une propriete (decision humaine). Incremente la revision :
        l'ecart avec applied_rev = ce qui reste a executer."""
        self.acted[key] = value
        self.acted_rev += 1
        self.touch(f"acte {key}={_short(value)}")
        return self

    def unact(self, key):
        if key in self.acted:
            del self.acted[key]
            self.acted_rev += 1
            self.touch(f"retire acte {key}")
        return self

    def pending(self):
        """Y a-t-il de l'acte non encore execute par le serveur ?"""
        return self.acted_rev > self.applied_rev

    def mark_applied(self):
        """Le serveur a execute l'acte courant."""
        self.applied_rev = self.acted_rev
        self.touch("acte applique (execute)")
        return self

    def acted_list(self):
        """Liste lisible des proprietes actees (pour l'ecran de confirmation)."""
        return [f"{k} = {_short(v)}" for k, v in self.acted.items()]

    # --- serialisation -----------------------------------------------------
    def to_dict(self):
        d = {k: getattr(self, k) for k in self.__slots__ if k != "candidates"}
        d["candidates"] = [c.to_dict() for c in self.candidates]
        return d

    @classmethod
    def from_dict(cls, d):
        obj = cls(d["title"], d["body"], d.get("origin", O_MANUAL),
                  d.get("source", ""), id=d.get("id"))
        for k in ("domains", "status", "score", "created", "updated", "history",
                  "acted", "chosen", "acted_rev", "applied_rev", "issue"):
            if k in d:
                object.__setattr__(obj, k, d[k])
        obj.candidates = [Candidate.from_dict(c) for c in d.get("candidates", [])]
        return obj

    def save(self, base_dir):
        p = Path(base_dir)
        p.mkdir(parents=True, exist_ok=True)
        f = p / f"{self.id}.json"
        f.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        return str(f)

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))

    def as_markdown(self):
        """Rendu lisible pour une Issue / carte de board : les 3 couches."""
        doms = ", ".join(self.domains) or "(non route)"
        lines = [f"# {self.title}",
                 f"_id {self.id} · origine {self.origin} · statut "
                 f"**{self.status}** · score {self.score:.2f} · domaines {doms}_",
                 "", self.body, ""]
        if self.acted:
            star = " (en attente d'execution)" if self.pending() else ""
            lines.append(f"## Acte{star}")
            for k, v in self.acted.items():
                lines.append(f"- **{k}** : {_short(v)}")
            lines.append("")
        if self.candidates:
            lines.append(f"## Candidats d'inference ({len(self.candidates)})")
            for c in self.candidates:
                mark = " ✓ retenu" if c.id == self.chosen else ""
                lines.append(f"- `{c.id}` (score {c.score:.2f}){mark} — "
                             f"{c.summary}")
        return "\n".join(lines)

    def __setattr__(self, name, value):
        if name == "score":
            try:
                value = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                raise ValueError(f"score non numerique: {value!r}") from None
        object.__setattr__(self, name, value)

    def __repr__(self):
        p = "*" if self.pending() else ""
        return (f"<Idea {self.id} [{self.status}]{p} score={self.score:.2f} "
                f"cand={len(self.candidates)} acte={len(self.acted)} "
                f"'{self.title[:32]}'>")


def _short(v, n=60):
    s = str(v).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


# --------------------------------------------------------------------------- #
# adaptateurs d'entree -> fiche idee (le "repack", en TEXTE)
# --------------------------------------------------------------------------- #
def from_file(path):
    """N'importe quel fichier TEXTE pushe -> fiche idee. (Les binaires/PDF/
    images passent d'abord par un extracteur : VLM/Whisper/pdf -> texte.)"""
    text = Path(path).read_text(errors="replace")
    title = _first_line(text) or os.path.basename(path)
    return Idea(title=title, body=text, origin=O_PUSH,
                source=os.path.basename(path))


def from_boot_report(health_path="/etc/health.json", diag_path=None):
    """Observation de boot -> idee. Unifie diagnostic et ideation : un boot
    problematique devient une idee a traiter dans le meme flux."""
    parts, issues = [], []
    try:
        h = json.loads(Path(health_path).read_text())
        parts.append(f"pool {h.get('pool')} = {h.get('pool_state')}")
        if not h.get("memory_ok", True):
            issues += h.get("memory_msgs", [])
        if (h.get("pool_state") or "").upper() in ("DEGRADED", "FAULTED",
                                                   "UNAVAIL"):
            issues.append(f"pool {h.get('pool_state')}")
    except (OSError, json.JSONDecodeError):
        pass
    if diag_path:
        try:
            d = json.loads(Path(diag_path).read_text())
            for it in d.get("issues", []):
                if it.get("severity") in ("CRITIQUE", "ATTENTION"):
                    issues.append(f"[{it['severity']}] {it['key']}: {it['message']}")
        except (OSError, json.JSONDecodeError):
            pass
    body = ("Observation de boot.\n\nEtat:\n  " + "\n  ".join(parts or ["(rien)"])
            + "\n\nAnomalies:\n  " + ("\n  ".join(issues) if issues else "(aucune)"))
    title = ("Boot OK" if not issues
             else f"Boot: {len(issues)} anomalie(s) a examiner")
    idea = Idea(title=title, body=body, origin=O_BOOT, source="boot-report")
    # un boot sain est peu prioritaire ; des anomalies le remontent
    idea.score = 0.2 if not issues else min(1.0, 0.4 + 0.15 * len(issues))
    return idea


def from_note(title, body):
    return Idea(title=title, body=body, origin=O_MANUAL, source="note")


def _first_line(text):
    for ln in text.splitlines():
        ln = ln.strip().lstrip("# ").strip()
        if ln:
            return ln[:80]
    return ""


# --------------------------------------------------------------------------- #
# moteur
# --------------------------------------------------------------------------- #
class BrainstormEngine:
    """Orchestre route -> develop via les pipelines du registre + le RAG.

    rag_engine : instance rag.RagEngine (pour build_context), ou None.
    router/llm : pipelines (kind router/llm), crees a la demande si None.
    """

    def __init__(self, store=None, rag_engine=None,
                 router=None, llm=None,
                 router_model=None, llm_model=None):
        self.store = store or _autoboot_dir("brainstorm")
        self.rag = rag_engine
        self._router = router
        self._llm = llm
        self._router_model = router_model or _model("qwen2.5-0.5b-instruct-ov")
        self._llm_model = llm_model or _model("qwen3-30b-a3b-int4-ov")

    @property
    def router(self):
        if self._router is None:
            self._router = ov_pipelines.create("router", self._router_model)
        return self._router

    @property
    def llm(self):
        if self._llm is None:
            self._llm = ov_pipelines.create("llm", self._llm_model)
        return self._llm

    # --- routage : choisir le(s) domaine(s) RAG ---------------------------
    def route(self, idea, domains=None):
        """Choisit les domaines pertinents. domains=None -> ceux du RAG."""
        labels = domains or (self.rag.domains() if self.rag else [])
        if not labels:
            idea.domains = []
            idea.touch("route: aucun domaine disponible")
            return idea
        text = f"{idea.title}\n{idea.body[:800]}"
        picked = self.router.route(text, labels, multi=True)
        idea.domains = picked if isinstance(picked, list) else [picked]
        idea.touch(f"route -> {idea.domains}")
        return idea

    # --- developpement : LLM + contexte RAG -> CANDIDAT -------------------
    def develop(self, idea, top_n=5, max_tokens=1024):
        """Produit un CANDIDAT (couche exploration). NE touche ni l'etat ni
        l'acte : l'inference peut etre relancee pour generer d'autres candidats
        sans rien remettre en cause de ce qui est acte."""
        ctx, sources = ("", [])
        if self.rag and idea.domains:
            ctx, sources = self.rag.build_context(
                f"{idea.title}\n{idea.body}", domains=idea.domains, top_n=top_n)
        prompt = self._develop_prompt(idea, ctx)
        out = self.llm.generate(prompt, max_tokens=max_tokens, temperature=0.2)
        summary = _first_line(out) or "solution"
        cand = idea.add_candidate(summary=summary, body=out,
                                  sources=sources, score=idea.score)
        return idea, cand

    @staticmethod
    def _develop_prompt(idea, ctx):
        base = (f"Idee a developper :\nTitre: {idea.title}\n\n{idea.body}\n\n")
        if ctx:
            base += ("Contexte documentaire local (cite [n]) :\n" + ctx + "\n\n")
        base += ("Developpe cette idee de facon technique et concrete : "
                 "faisabilite, etapes, risques. Si le contexte est pertinent, "
                 "appuie-toi dessus en citant [n].")
        return base

    # --- traitement par lot (genere la progression) -----------------------
    def process(self, ideas, domains=None, develop=True, save=True):
        """Route (+ developpe) une sequence d'idees. Genere (i, total, Idea)."""
        ideas = list(ideas)
        total = len(ideas)
        for i, idea in enumerate(ideas, 1):
            self.route(idea, domains=domains)
            if develop:
                self.develop(idea)
            if save:
                idea.save(self.store)
            yield (i, total, idea)

    # --- board : acces aux fiches stockees --------------------------------
    def __iter__(self):
        base = Path(self.store)
        if base.is_dir():
            for p in sorted(base.glob("*.json")):
                try:
                    d = json.loads(p.read_text())
                    if "title" in d and "body" in d:    # fiche idee valide
                        yield Idea.from_dict(d)
                except (OSError, json.JSONDecodeError):
                    continue

    def __getitem__(self, idea_id):
        p = Path(self.store) / f"{idea_id}.json"
        if not p.exists():
            existing = [q.stem for q in Path(self.store).glob("*.json")]
            raise KeyError(
                f"idee '{idea_id}' introuvable dans {self.store} ; "
                f"{len(existing)} fiche(s) presente(s)")
        return Idea.load(p)

    def by_status(self, status):
        if status not in VALID_STATUS:
            raise ValueError(
                f"statut invalide '{status}' ; attendu {VALID_STATUS}")
        return [idea for idea in self if idea.status == status]


# --------------------------------------------------------------------------- #
# CLI minimale
# --------------------------------------------------------------------------- #
def main():
    import argparse
    ap = argparse.ArgumentParser(description="flux brainstorm (local)")
    ap.add_argument("--store", default=None,
                    help="dossier des idees (defaut: ~/.autoboot/brainstorm)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("add-file"); pf.add_argument("path")
    pb = sub.add_parser("add-boot")
    pb.add_argument("--health", default="/etc/health.json")
    pb.add_argument("--diag", default=None)
    pl = sub.add_parser("list")
    pl.add_argument("--status", default=None)
    a = ap.parse_args()

    eng = BrainstormEngine(store=a.store)

    if a.cmd == "add-file":
        idea = from_file(a.path)
        for i, total, it in eng.process([idea]):
            print(f"  {i}/{total} {it!r} -> {eng.store}/{it.id}.json")
    elif a.cmd == "add-boot":
        idea = from_boot_report(a.health, a.diag)
        for i, total, it in eng.process([idea]):
            print(f"  {it!r}")
    elif a.cmd == "list":
        items = eng.by_status(a.status) if a.status else list(eng)
        for it in items:
            print(f"  {it!r}  domaines={it.domains}")


if __name__ == "__main__":
    main()

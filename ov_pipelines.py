#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
ov_pipelines.py — registre extensible de pipelines d'inference (metaclasse).

Principe : ajouter un modele secondaire = ecrire une sous-classe de Pipeline
avec un attribut de classe `kind`. La metaclasse PipelineMeta l'enregistre
AUTOMATIQUEMENT au moment de la definition de la classe. Aucun autre fichier
a modifier ; rag.py et les futurs scripts consomment les pipelines via le
registre, sans connaitre les classes concretes.

Deux back-ends interchangeables (meme interface) :
  - OpenVINOBackend : production, openvino_genai sur plugin CPU
  - StubBackend     : deterministe, stdlib pure, pour dev/tests hors-cible

Choix du back-end : variable d'env RAG_BACKEND = auto|openvino|stub
  (auto = openvino si importable, sinon stub).

Style : metaclasse pour l'auto-enregistrement, magic methods avec messages
d'acces explicites, yield sur l'iteration du registre.
"""
import hashlib
import math
import os
import re


def _tokenize(text):
    return re.findall(r"[a-z0-9]+", str(text).lower())


# --------------------------------------------------------------------------- #
# metaclasse a auto-enregistrement
# --------------------------------------------------------------------------- #
class PipelineMeta(type):
    """Enregistre toute sous-classe possedant un attribut `kind` non vide."""
    registry = {}        # kind -> classe

    def __new__(mcs, name, bases, ns):
        cls = super().__new__(mcs, name, bases, ns)
        kind = ns.get("kind")
        if kind:
            if kind in mcs.registry:
                raise TypeError(
                    f"kind '{kind}' deja enregistre par "
                    f"{mcs.registry[kind].__name__}")
            mcs.registry[kind] = cls
        return cls


class _Registry:
    """Acces au registre avec messages explicites (magic methods)."""

    def __getitem__(self, kind):
        try:
            return PipelineMeta.registry[kind]
        except KeyError:
            raise KeyError(
                f"pipeline inconnu '{kind}' ; disponibles : "
                f"{self.kinds()}") from None

    def __contains__(self, kind):
        return kind in PipelineMeta.registry

    def __iter__(self):
        for kind in sorted(PipelineMeta.registry):
            yield kind, PipelineMeta.registry[kind]

    def __len__(self):
        return len(PipelineMeta.registry)

    def kinds(self):
        return sorted(PipelineMeta.registry)


registry = _Registry()


def create(kind, model_path, device="CPU", backend=None, **opts):
    """Fabrique : instancie le pipeline enregistre sous `kind`."""
    return registry[kind](model_path, device=device, backend=backend, **opts)


# --------------------------------------------------------------------------- #
# back-ends
# --------------------------------------------------------------------------- #
class Backend:
    name = "base"

    def load(self, kind, model_path, device, **opts):
        raise NotImplementedError

    def embed(self, handle, texts):
        raise NotImplementedError(f"{self.name}: embed non supporte")

    def rerank(self, handle, query, docs):
        raise NotImplementedError(f"{self.name}: rerank non supporte")

    def generate(self, handle, prompt, max_tokens, temperature):
        raise NotImplementedError(f"{self.name}: generate non supporte")

    def vlm(self, handle, prompt, image_paths):
        raise NotImplementedError(f"{self.name}: vlm non supporte")

    def transcribe(self, handle, audio_path):
        raise NotImplementedError(f"{self.name}: transcribe non supporte")


class OpenVINOBackend(Backend):
    """Production. Les noms de methode GenAI peuvent varier selon la version ;
    ils sont isoles ici (un seul endroit a ajuster si l'API bouge)."""
    name = "openvino"

    def load(self, kind, model_path, device, **opts):
        import openvino_genai as ov
        ctor = {
            "llm":       getattr(ov, "LLMPipeline", None),
            "embedding": getattr(ov, "TextEmbeddingPipeline", None),
            "rerank":    getattr(ov, "TextRerankPipeline", None),
            "vlm":       getattr(ov, "VLMPipeline", None),
            "whisper":   getattr(ov, "WhisperPipeline", None),
        }.get(kind)
        if ctor is None:
            raise RuntimeError(
                f"openvino_genai n'expose pas de pipeline pour kind '{kind}'")
        return ctor(str(model_path), device)

    def embed(self, handle, texts):
        for meth in ("embed_documents", "embed", "encode"):
            fn = getattr(handle, meth, None)
            if fn:
                return [list(v) for v in fn(list(texts))]
        raise RuntimeError("API embedding GenAI inconnue (methode introuvable)")

    def rerank(self, handle, query, docs):
        for meth in ("rerank", "score"):
            fn = getattr(handle, meth, None)
            if fn:
                res = fn(query, list(docs))
                # tolere [(doc, score)] ou [score]
                out = []
                for i, item in enumerate(res):
                    if isinstance(item, (tuple, list)) and len(item) == 2:
                        out.append((item[0], float(item[1])))
                    else:
                        out.append((docs[i], float(item)))
                return out
        raise RuntimeError("API rerank GenAI inconnue (methode introuvable)")

    def generate(self, handle, prompt, max_tokens, temperature):
        cfg = handle.get_generation_config()
        cfg.max_new_tokens = max_tokens
        if temperature and temperature > 0:
            cfg.do_sample = True
            cfg.temperature = float(temperature)
        return str(handle.generate(prompt, cfg))

    def vlm(self, handle, prompt, image_paths):
        return str(handle.generate(prompt, images=list(image_paths)))

    def transcribe(self, handle, audio_path):
        return str(handle.generate(audio_path))


class StubBackend(Backend):
    """Deterministe, sans dependance. Embeddings = sac-de-mots hache normalise
    (cosinus = recouvrement lexical). Permet de tester tout le RAG hors-cible.
    hashlib (pas hash()) -> stable entre processus."""
    name = "stub"
    DIM = 128

    def load(self, kind, model_path, device, **opts):
        return {"kind": kind, "path": str(model_path), "device": device}

    @classmethod
    def _vec(cls, text):
        v = [0.0] * cls.DIM
        for w in _tokenize(text):
            d = hashlib.md5(w.encode()).digest()
            idx = int.from_bytes(d[:4], "big") % cls.DIM
            v[idx] += 1.0 if (d[4] & 1) else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, handle, texts):
        return [self._vec(t) for t in texts]

    def rerank(self, handle, query, docs):
        qs = set(_tokenize(query))
        denom = len(qs) or 1
        return [(d, len(qs & set(_tokenize(d))) / denom) for d in docs]

    def generate(self, handle, prompt, max_tokens, temperature):
        return f"[stub:{handle['path']}] " + " ".join(_tokenize(prompt)[:40])

    def vlm(self, handle, prompt, image_paths):
        return f"[stub-vlm] {len(image_paths)} image(s) ; prompt={prompt[:60]}"

    def transcribe(self, handle, audio_path):
        return f"[stub-whisper] transcription de {audio_path}"


def default_backend():
    choice = os.environ.get("RAG_BACKEND", "auto").lower()
    if choice == "stub":
        return StubBackend()
    if choice in ("openvino", "ov"):
        return OpenVINOBackend()
    try:
        import openvino_genai  # noqa: F401
        return OpenVINOBackend()
    except Exception:
        return StubBackend()


# --------------------------------------------------------------------------- #
# pipelines
# --------------------------------------------------------------------------- #
class Pipeline(metaclass=PipelineMeta):
    """Base non enregistree (kind=None). Charge le modele a la demande."""
    kind = None
    #: methodes operationnelles exposees (pour les messages d'__getattr__)
    ops = ()

    def __init__(self, model_path, device="CPU", backend=None, **opts):
        self.model_path = model_path
        self.device = device
        self.opts = opts
        self.backend = backend or default_backend()
        self._handle = None

    @property
    def loaded(self):
        return self._handle is not None

    def load(self):
        if self._handle is None:
            self._handle = self.backend.load(
                self.kind, self.model_path, self.device, **self.opts)
        return self

    def __getattr__(self, name):
        # appele seulement si l'attribut est introuvable normalement
        kind = type(self).__dict__.get("kind", "?")
        ops = type(self).__dict__.get("ops", ())
        raise AttributeError(
            f"'{type(self).__name__}' (kind={kind}) n'a pas d'attribut "
            f"'{name}'. Operations disponibles : {ops or '(aucune)'}")

    def __repr__(self):
        state = "charge" if self.loaded else "non charge"
        return (f"<{type(self).__name__} kind={self.kind} "
                f"backend={self.backend.name} {state}>")


class LLMPipeline(Pipeline):
    kind = "llm"
    ops = ("generate",)

    def generate(self, prompt, max_tokens=1024, temperature=0.1):
        self.load()
        return self.backend.generate(self._handle, prompt, max_tokens, temperature)


class EmbeddingPipeline(Pipeline):
    kind = "embedding"
    ops = ("embed", "embed_query")

    def embed(self, texts):
        self.load()
        if isinstance(texts, str):
            texts = [texts]
        return self.backend.embed(self._handle, list(texts))

    def embed_query(self, text):
        return self.embed([text])[0]


class RerankPipeline(Pipeline):
    kind = "rerank"
    ops = ("rerank",)

    def rerank(self, query, docs, top_k=None):
        self.load()
        scored = self.backend.rerank(self._handle, query, list(docs))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:top_k] if top_k else scored


class VLMPipeline(Pipeline):
    kind = "vlm"
    ops = ("describe",)

    def describe(self, prompt, image_paths):
        self.load()
        return self.backend.vlm(self._handle, prompt, list(image_paths))


class WhisperPipeline(Pipeline):
    kind = "whisper"
    ops = ("transcribe",)

    def transcribe(self, audio_path):
        self.load()
        return self.backend.transcribe(self._handle, audio_path)


class RouterPipeline(Pipeline):
    """Routeur zero-shot : classe un texte dans un des labels fournis.
    S'appuie sur un petit LLM (ex. Qwen2.5-0.5B-Instruct) via le meme back-end.
    Reponse attendue = un seul label ; on retombe sur le 1er label si la
    reponse est hors-liste (jamais d'echec dur)."""
    kind = "router"
    ops = ("route",)

    def route(self, text, labels, multi=False):
        self.load()
        labels = list(labels)
        joined = "|".join(labels)
        instr = ("Classe le texte suivant. Reponds UNIQUEMENT par "
                 + ("un ou plusieurs labels separes par des virgules"
                    if multi else "un seul label")
                 + f" parmi : {joined}. Aucun autre mot.\n\nTexte:\n")
        raw = self.backend.generate(self._handle, instr + str(text),
                                    max_tokens=32, temperature=0.0)
        found = [l for l in labels if l.lower() in str(raw).lower()]
        if not found:
            return [labels[0]] if multi else labels[0]
        return found if multi else found[0]


if __name__ == "__main__":
    print("back-end par defaut :", default_backend().name)
    print("pipelines enregistres :")
    for kind, cls in registry:
        print(f"  {kind:10} -> {cls.__name__}  ops={cls.ops}")

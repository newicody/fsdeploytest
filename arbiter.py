#!/usr/bin/env python3
"""arbiter.py -- orchestrateur d'inference (l'arbitre OpenVINO).

Un artefact 'needs:inference' fait travailler PLUSIEURS workers EN PARALLELE
(inference locale OpenVINO + Claude/Gemini si des cles sont configurees), puis
l'ARBITRE -- un modele OpenVINO local -- trie/synthetise les avis (ou donne le
sien). Le resultat repart par dispatch.post_feedback -> board -> Copilot/toi.
Reutilise ov_pipelines pour l'inference locale ; AUCUNE infra parallele.

Config infra.conf :
    [arbiter]
        workers = local, claude, gemini   # workers a lancer (defaut: local)
        model = /var/lib/models/<arbitre>  # modele OpenVINO LLM de l'arbitre
        device = CPU
        max_tokens = 1024
        claude_model = claude-sonnet-4-6
        gemini_model = gemini-2.0-flash
Cles API (JAMAIS dans l'ini) : ANTHROPIC_API_KEY, GEMINI_API_KEY (env). Un worker
sans cle/modele est SAUTE proprement (ok=False), non bloquant.
"""
import os
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _prompt(artifact):
    """Construit le prompt d'un artefact (mode/domaine/axe/titre/corps)."""
    parts = [
        f"Projet/mode : {artifact.get('mode', '')}",
        f"Axe : {artifact.get('axis', '')} / Domaine : {artifact.get('domain', '')}",
        f"Type : {artifact.get('type', '')}",
        f"Titre : {artifact.get('title', '')}",
        "",
        artifact.get("body", "") or "",
    ]
    return "\n".join(p for p in parts if p is not None)


# --------------------------------------------------------------------------- #
# workers
# --------------------------------------------------------------------------- #
class Worker:
    name = "base"

    def run(self, artifact, log=print):
        raise NotImplementedError


class LocalWorker(Worker):
    """Inference locale via ov_pipelines (OpenVINO, CPU). Le coeur 'maison'."""
    name = "local"

    def __init__(self, model, device="CPU", max_tokens=1024):
        self.model, self.device, self.max_tokens = model, device, max_tokens

    def run(self, artifact, log=print):
        if not self.model:
            return {"worker": self.name, "ok": False, "error": "aucun modele local"}
        try:
            import ov_pipelines
            pipe = ov_pipelines.create("llm", self.model, device=self.device)
            out = pipe.generate(_prompt(artifact), max_tokens=self.max_tokens)
            return {"worker": self.name, "ok": True, "opinion": out}
        except Exception as e:
            return {"worker": self.name, "ok": False, "error": str(e)}


class ClaudeWorker(Worker):
    name = "claude"
    API = "https://api.anthropic.com/v1/messages"

    def __init__(self, model="claude-sonnet-4-6", max_tokens=1024):
        self.model, self.max_tokens = model, max_tokens

    def run(self, artifact, log=print):
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return {"worker": self.name, "ok": False, "error": "ANTHROPIC_API_KEY absent"}
        body = json.dumps({
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": _prompt(artifact)}],
        }).encode()
        req = urllib.request.Request(self.API, data=body, headers={
            "content-type": "application/json", "x-api-key": key,
            "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            txt = "".join(b.get("text", "") for b in data.get("content", [])
                          if b.get("type") == "text")
            return {"worker": self.name, "ok": True, "opinion": txt}
        except Exception as e:
            return {"worker": self.name, "ok": False, "error": str(e)}


class GeminiWorker(Worker):
    name = "gemini"

    def __init__(self, model="gemini-2.0-flash"):
        self.model = model

    def run(self, artifact, log=print):
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            return {"worker": self.name, "ok": False, "error": "GEMINI_API_KEY absent"}
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent?key={key}")
        body = json.dumps(
            {"contents": [{"parts": [{"text": _prompt(artifact)}]}]}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            txt = "".join(p.get("text", "") for c in data.get("candidates", [])
                          for p in c.get("content", {}).get("parts", []))
            return {"worker": self.name, "ok": True, "opinion": txt}
        except Exception as e:
            return {"worker": self.name, "ok": False, "error": str(e)}


class StubWorker(Worker):
    """Worker deterministe (tests hors-cible : ni reseau ni modele)."""
    name = "stub"

    def run(self, artifact, log=print):
        return {"worker": self.name, "ok": True,
                "opinion": f"[stub] avis sur '{artifact.get('title', '')}' "
                           f"(mode {artifact.get('mode', '')})"}


_REGISTRY = {"local": LocalWorker, "claude": ClaudeWorker,
             "gemini": GeminiWorker, "stub": StubWorker}


# --------------------------------------------------------------------------- #
# config + assemblage
# --------------------------------------------------------------------------- #
def _arbiter_cfg(infra_path=None):
    try:
        import manager_git
        cfg = manager_git._infra(infra_path) or {}
    except Exception:
        cfg = {}
    a = (cfg.get("arbiter", {}) or {})

    def _csv(v, d):
        if v is None:
            return d
        return v if isinstance(v, list) else [x.strip() for x in v.split(",") if x.strip()]

    return {
        "workers": _csv(a.get("workers"), ["local"]),
        "model": a.get("model", os.environ.get("ARBITER_MODEL", "")),
        "device": a.get("device", "CPU"),
        "max_tokens": int(a.get("max_tokens", 1024) or 1024),
        "claude_model": a.get("claude_model", "claude-sonnet-4-6"),
        "gemini_model": a.get("gemini_model", "gemini-2.0-flash"),
    }


def _build_workers(cfg):
    out = []
    for name in cfg["workers"]:
        cls = _REGISTRY.get(name)
        if cls is LocalWorker:
            out.append(LocalWorker(cfg["model"], cfg["device"], cfg["max_tokens"]))
        elif cls is ClaudeWorker:
            out.append(ClaudeWorker(cfg["claude_model"], cfg["max_tokens"]))
        elif cls is GeminiWorker:
            out.append(GeminiWorker(cfg["gemini_model"]))
        elif cls is StubWorker:
            out.append(StubWorker())
    return out


# --------------------------------------------------------------------------- #
# arbitre
# --------------------------------------------------------------------------- #
def arbitrate(artifact, opinions, cfg, log=print):
    """L'ARBITRE : un modele OpenVINO local lit les avis et SYNTHETISE/tranche.
    Repli sans modele : fusion structuree des avis valides."""
    valid = [o for o in opinions if o.get("ok")]
    if cfg.get("model"):
        try:
            import ov_pipelines
            pipe = ov_pipelines.create("llm", cfg["model"], device=cfg["device"])
            prompt = ("Tu es l'arbitre. Voici une demande et plusieurs avis d'IA. "
                      "Trie, ecarte les erreurs, et donne une SYNTHESE actionnable.\n\n"
                      f"DEMANDE :\n{_prompt(artifact)}\n\nAVIS :\n"
                      + "\n".join(f"- [{o['worker']}] {o.get('opinion', '')}"
                                  for o in valid))
            synth = pipe.generate(prompt, max_tokens=cfg["max_tokens"])
            return {"by": "openvino-model", "synthesis": synth}
        except Exception as e:
            log(f"[arbiter] modele d'arbitrage indisponible ({e}) -> fusion")
    synth = "\n".join(f"- [{o['worker']}] {o.get('opinion', '')}" for o in valid) \
        or "(aucun avis exploitable)"
    return {"by": "merge", "synthesis": synth}


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def orchestrate(artifact, infra_path=None, log=print):
    """needs:inference -> workers EN PARALLELE -> arbitrage -> resultat (dict
    pret pour post_feedback : porte 'synthesis')."""
    cfg = _arbiter_cfg(infra_path)
    workers = _build_workers(cfg)
    if not workers:
        return {"action": "inference", "via": "openvino", "number": artifact.get("number"),
                "mode": artifact.get("mode"), "synthesis": "(aucun worker configure)"}
    with ThreadPoolExecutor(max_workers=max(1, len(workers))) as ex:
        opinions = list(ex.map(lambda w: w.run(artifact, log), workers))
    for o in opinions:
        log(f"[arbiter] worker {o['worker']} : "
            + ("ok" if o.get("ok") else f"KO ({o.get('error')})"))
    verdict = arbitrate(artifact, opinions, cfg, log)
    return {
        "action": "inference", "via": "openvino", "number": artifact.get("number"),
        "mode": artifact.get("mode"),
        "workers": [o["worker"] for o in opinions],
        "opinions": opinions, "arbiter": verdict.get("by"),
        "synthesis": verdict.get("synthesis", ""),
    }


if __name__ == "__main__":
    import sys
    art = {"mode": "garage", "axis": "operations", "domain": "reparation",
           "type": "question", "number": 1, "title": "bruit moteur a froid",
           "body": "claquement 2s au demarrage a froid, disparait apres."}
    print(json.dumps(orchestrate(art), indent=2, ensure_ascii=False))

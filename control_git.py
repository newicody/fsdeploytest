#!/usr/bin/env python3
"""control_git.py -- axe GitOps DESCENDANT (depot de controle -> machine).

Symetrique de manager_git (montant : la machine POUSSE son audit). Ici la machine
LIT un etat DESIRE declaratif depuis un depot de CONTROLE (ecrit par l'humain) et
le reconcilie (cf. kernel_watch). Principes, alignes sur manager_git :
  - Depot de controle SEPARE du manager : proprietaires d'ecriture distincts
    (l'humain ecrit le controle, la machine ecrit le manager) -> aucun conflit.
  - La machine ne POUSSE JAMAIS sur le depot de controle (lecture seule stricte).
  - Clone/pull dans <manager>/control : cache DURABLE (boot_pool = mirror) donc
    TOLERANT a l'offline (dernier etat desire connu reutilise si le pull echoue).
  - Token injecte uniquement dans l'URL (ephemere), jamais dans .git/config.
  - DESACTIVE si [control] git_remote est vide (return None) : opt-in.
  - Reutilise les helpers de manager_git (pas d'infrastructure parallele).

Config infra.conf :
    [control]
        git_remote =            # depot de controle (vide = GitOps descendant off)
        git_branch = main
        path = desired.conf     # fichier d'etat desire dans le depot

Etat desire (desired.conf, configobj -- ecrit par l'humain) :
    [kernel]
        target = latest         # latest | <version exacte> | pinned:<version>
        approve_build = false   # autorisation DECLARATIVE de construire vers target
        rollback_to =           # version vers laquelle revenir (vide = aucun)
        config_policy = review  # review | auto (LLM .config : revue humaine ou auto)
    [note]
        text =
"""
import os
import subprocess
from pathlib import Path

import manager_git   # _infra / manager_root / _auth_url reutilises


def _control_config():
    """(remote, branch, path) depuis [control]. remote vide = GitOps off."""
    cfg = manager_git._infra()
    c = (cfg.get("control", {}) or {}) if cfg else {}
    return ((c.get("git_remote") or "").strip(),
            (c.get("git_branch") or "main").strip(),
            (c.get("path") or "desired.conf").strip())


def _clone_dir():
    """Cache local du depot de controle (durable, sous le manager)."""
    return manager_git.manager_root() / "control"


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "oui")


def pull(log=print):
    """Clone (1re fois) ou met a jour le depot de controle dans <manager>/control.
    LECTURE SEULE. Retourne le chemin local s'il est disponible (meme via cache si
    offline), None si GitOps descendant desactive ([control] git_remote vide)."""
    remote, branch, _ = _control_config()
    if not remote:
        return None
    dest = _clone_dir()
    url = manager_git._auth_url(remote)
    try:
        if (dest / ".git").is_dir():
            r = subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1",
                                "origin", branch],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                subprocess.run(["git", "-C", str(dest), "reset", "--hard",
                                f"origin/{branch}"],
                               capture_output=True, text=True, timeout=20)
                log("[control] depot de controle a jour")
            else:
                log("[control] fetch echoue -> cache local conserve "
                    f"({(r.stderr or '').strip()[:100]})")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "--depth", "1", "-b", branch,
                                url, str(dest)],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                log("[control] clone echoue : "
                    f"{(r.stderr or '').strip()[:100]}")
            else:
                log("[control] depot de controle clone")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"[control] git indisponible ({e}) -> cache local si present")
    return dest if (dest / ".git").is_dir() else None


def read_desired(log=print):
    """Pull (best effort) + lit/parse l'etat desire. Retourne un dict NORMALISE,
    None si GitOps descendant desactive ou aucun etat lisible (ni online ni cache).
    Tolerant offline : reutilise le clone en cache. La machine NE POUSSE JAMAIS."""
    remote, _, relpath = _control_config()
    if not remote:
        return None
    dest = pull(log=log)
    if not dest:
        return None
    f = Path(dest) / relpath
    if not f.is_file():
        log(f"[control] etat desire absent : {relpath}")
        return None
    try:
        from configobj import ConfigObj
        d = ConfigObj(str(f))
    except Exception as e:
        log(f"[control] etat desire illisible ({e})")
        return None
    k = (d.get("kernel", {}) or {})
    return {
        "kernel": {
            "target": (k.get("target") or "latest").strip(),
            "approve_build": _truthy(k.get("approve_build", "false")),
            "rollback_to": (k.get("rollback_to") or "").strip(),
            "config_policy": (k.get("config_policy") or "review").strip().lower(),
        },
        "note": ((d.get("note", {}) or {}).get("text") or "").strip(),
        "_source": str(f),
    }


if __name__ == "__main__":
    import json
    st = read_desired()
    if st is None:
        print("GitOps descendant desactif ([control] git_remote vide) "
              "ou etat desire illisible.")
    else:
        print(json.dumps(st, indent=2, ensure_ascii=False))

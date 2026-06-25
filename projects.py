#!/usr/bin/env python3
"""projects.py -- couche d'INGESTION multi-depots (artefacts via l'API GitHub).

Generalise le board mono-depot existant en une couche projet-agnostique :
  - REGISTRE [projects] dans infra.conf : N depots, chacun avec son MODE projet
    (kernel, garage, science, ...). La machine interroge chaque depot via l'API
    GitHub (issues = artefacts, labels = type/etat/route), puis dispatche selon le
    MODE + les labels (cf. dispatch a venir : route directe vs orchestrateur).
  - REUTILISE github_board (GitHubTransport REST + Board) : AUCUNE infra parallele.
    Le transport reel est mono-depot (repo='owner/name') ; ici on en instancie un
    PAR entree du registre.
  - Les ARTEFACTS sont des Issues (corps Markdown + labels), jamais de binaire,
    cf. la convention etat<->label deja en place (state:idea|wip|dev|prod|drop).

Taxonomie de labels (etend l'existant) :
    type:<...>     type d'artefact         (kernel-validation, idea, question, ...)
    state:<...>    cycle de vie            (idea|wip|dev|prod|drop -- existant)
    route:direct   bypass de l'orchestrateur d'inference (handler direct du mode)
    needs:inference  demande une inference via le dispatcheur local

Registre infra.conf :
    [projects]
        [[kernel]]
            repo = newicody/fsdeploytest
            mode = kernel
        [[garage]]
            repo = newicody/mavoiture
            mode = garage
"""
import os

TYPE_PREFIX = "type:"        # type d'artefact
STATE_PREFIX = "state:"      # cycle de vie (deja utilise par github_board)
AXIS_PREFIX = "axis:"        # axe transversal (taxonomy.conf)
DOMAIN_PREFIX = "domain:"    # sous-domaine du mode (taxonomy.conf)
ROUTE_DIRECT = "route:direct"   # bypass orchestrateur -> handler direct du mode
NEEDS_INFER = "needs:inference"


def _infra(path=None):
    """Charge infra.conf (reutilise la resolution de manager_git si dispo)."""
    try:
        import manager_git
        cfg = manager_git._infra(path)
        if cfg is not None:
            return cfg
    except Exception:
        pass
    for c in (path, os.environ.get("INFRA_CONF"), "/etc/infra.conf",
              "/infra.conf", "/sbin/infra.conf"):
        if c and os.path.exists(c):
            try:
                from configobj import ConfigObj
                return ConfigObj(c)
            except Exception:
                return None
    return None


def registry(infra_path=None):
    """Lit [projects] -> liste de dict {name, repo, owner, mode}. Vide si absent.
    Une entree sans 'repo' est ignoree (avec un nom de section comme libelle)."""
    cfg = _infra(infra_path)
    proj = (cfg.get("projects", {}) or {}) if cfg else {}
    out = []
    for name, sub in proj.items():
        if not isinstance(sub, dict):
            continue
        repo = (sub.get("repo") or "").strip()
        if not repo:
            continue
        mode = (sub.get("mode") or name).strip()
        owner = repo.split("/", 1)[0] if "/" in repo else ""
        out.append({"name": name, "repo": repo, "owner": owner, "mode": mode})
    return out


def board_for(entry, token=None):
    """github_board.Board pour une entree du registre (reutilise GitHubTransport).
    None si github_board indisponible ou pas de token (mode hors-ligne)."""
    try:
        import github_board as gb
    except Exception:
        return None
    try:
        return gb.Board(gb.GitHubTransport(entry["repo"], token=token))
    except Exception:
        return None


def _label_names(issue):
    """Labels d'une issue, que le transport renvoie des dict {name} (API reelle)
    ou des chaines (StubTransport)."""
    out = []
    for l in (issue.get("labels") or []):
        out.append(l["name"] if isinstance(l, dict) and "name" in l else str(l))
    return out


def classify(issue, repo="", mode=""):
    """Normalise une Issue en ARTEFACT projet-agnostique a partir de ses labels,
    et valide mode/axis/domain/state contre la taxonomie (permissif : un inconnu
    est signale via 'valid', jamais rejete)."""
    labels = _label_names(issue)

    def _pick(prefix):
        return next((l[len(prefix):] for l in labels if l.startswith(prefix)), "")

    atype = _pick(TYPE_PREFIX)
    state = _pick(STATE_PREFIX)
    axis = _pick(AXIS_PREFIX)
    domain = _pick(DOMAIN_PREFIX)
    try:
        import taxonomy
        valid = taxonomy.validate(mode=mode, axis=axis, domain=domain, state=state)
    except Exception:
        valid = {}
    return {
        "repo": repo,
        "mode": mode,
        "number": issue.get("number"),
        "title": issue.get("title", ""),
        "body": issue.get("body", ""),
        "labels": labels,
        "type": atype,
        "state": state,
        "axis": axis,
        "domain": domain,
        "route_direct": ROUTE_DIRECT in labels,
        "needs_inference": NEEDS_INFER in labels,
        "valid": valid,
    }


def list_artifacts(entry, token=None, board=None, log=print):
    """Issues d'un depot -> artefacts classifies (tagges repo+mode). [] si le board
    est indisponible (offline/token absent) : non bloquant."""
    b = board or board_for(entry, token=token)
    if b is None:
        log(f"[projects] {entry['repo']} : board indisponible (token ? reseau ?)")
        return []
    try:
        issues = b.tp.list_issues()
    except Exception as e:
        log(f"[projects] {entry['repo']} : list_issues echoue ({e})")
        return []
    return [classify(i, repo=entry["repo"], mode=entry["mode"]) for i in issues]


def all_artifacts(infra_path=None, token=None, log=print):
    """Tous les artefacts de tous les depots du registre. Chaque artefact porte son
    repo + mode -> pret pour le dispatch (par mode + labels)."""
    out = []
    for entry in registry(infra_path):
        out.extend(list_artifacts(entry, token=token, log=log))
    return out


if __name__ == "__main__":
    import json
    reg = registry()
    print(f"{len(reg)} projet(s) dans le registre :")
    for e in reg:
        print(f"  - {e['name']}: {e['repo']} (mode {e['mode']})")
    arts = all_artifacts()
    print(f"\n{len(arts)} artefact(s) :")
    print(json.dumps(arts, indent=2, ensure_ascii=False)[:2000])

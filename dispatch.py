#!/usr/bin/env python3
"""dispatch.py -- routage des artefacts (couche generale GitOps).

Prend les artefacts classifies par projects.py et les ROUTE :
  - route:direct    -> handler DIRECT du mode (bypass orchestrateur),
  - needs:inference -> orchestrateur OpenVINO (ov_pipelines ; workers Claude/Gemini
                       ou avis direct -- brique a venir),
  - sinon           -> handler du mode (defaut).
Les handlers s'enregistrent PAR MODE via @register(mode). Chaque mode (kernel,
garage, science, ...) fournit son handler dans un module mode_<mode>.py charge ici.
Aucune logique metier reimplementee : les handlers DELEGUENT aux modules existants
(kernel_build, boot_confirm, ...).
"""
import importlib

HANDLERS = {}              # mode -> handler(artifact, ctx, log) -> dict decision
_MODES = ("kernel",)       # modules mode_<m> a charger (etendre au fil des modes)


def register(mode):
    """Decorateur d'enregistrement d'un handler de mode."""
    def deco(fn):
        HANDLERS[mode] = fn
        return fn
    return deco


def _load_modes(log=print):
    for m in _MODES:
        try:
            importlib.import_module(f"mode_{m}")
        except Exception as e:
            log(f"[dispatch] mode '{m}' non charge : {e}")


def to_orchestrator(artifact, ctx=None, log=print):
    """Route vers l'orchestrateur OpenVINO (foyer : ov_pipelines). Brique a venir :
    ici on SIGNALE le routage ; l'orchestration reelle (workers Claude/Gemini, avis
    direct, retravail des avis) est l'etape suivante. Un artefact route:direct ne
    passe JAMAIS ici."""
    log(f"[dispatch] artefact #{artifact.get('number')} (mode {artifact.get('mode')})"
        " -> orchestrateur OpenVINO (ov_pipelines) [routage signale, inference a venir]")
    return {"action": "inference", "via": "openvino",
            "reason": "needs:inference -> orchestrateur (ov_pipelines)",
            "number": artifact.get("number"), "mode": artifact.get("mode")}


def dispatch_one(artifact, ctx=None, log=print):
    """Route UN artefact selon route:direct / needs:inference / mode. Retourne la
    decision du handler (dict). Ne leve pas : un mode sans handler -> action none."""
    mode = artifact.get("mode", "")
    h = HANDLERS.get(mode)
    if artifact.get("route_direct"):
        if h:
            return h(artifact, ctx, log)
        log(f"[dispatch] route:direct mais aucun handler pour mode '{mode}'")
        return {"action": "none", "reason": f"no direct handler for '{mode}'",
                "number": artifact.get("number")}
    if artifact.get("needs_inference"):
        return to_orchestrator(artifact, ctx, log)
    if h:
        return h(artifact, ctx, log)
    return {"action": "none", "reason": f"no handler for mode '{mode}'",
            "number": artifact.get("number")}


def dispatch_all(infra_path=None, token=None, ctx=None, log=print):
    """Ingestion (projects) -> routage de TOUS les artefacts. Retourne la liste des
    decisions. Charge les modes au premier appel."""
    if not HANDLERS:
        _load_modes(log)
    import projects
    arts = projects.all_artifacts(infra_path, token, log)
    return [dispatch_one(a, ctx, log) for a in arts]


def post_feedback(entry, decision, board=None, token=None, log=print):
    """Reposte la decision de la machine sur l'artefact (Issue) -> BOUCLE DE
    RETRO-ACTION. Commentaire = ce que la machine a decide/fait ; un label
    'machine:<action>' marque le statut MACHINE. On NE touche PAS au 'state:' :
    c'est le kanban de l'HUMAIN (idea->wip->dev->prod). Best-effort, non bloquant."""
    num = decision.get("number")
    if num is None:
        return False
    import projects
    b = board or projects.board_for(entry, token=token)
    if b is None:
        log(f"[feedback] board indisponible pour {entry['repo']}")
        return False
    action = decision.get("action", "?")
    body = f"**Machine** : `{action}`"
    if decision.get("target"):
        body += f" -> cible `{decision['target']}`"
    if decision.get("reason"):
        body += f"\n\n{decision['reason']}"
    if "applied" in decision:
        body += f"\n\nbuild execute : {decision['applied']}"
    if decision.get("via") == "openvino":
        body += "\n\n(route -> orchestrateur OpenVINO ; inference a venir)"
    try:
        b.tp.add_comment(num, body)
        cur = b.tp.get_issue(num)
        labels = [l for l in projects._label_names(cur)
                  if not l.startswith("machine:")]
        labels.append(f"machine:{action}")
        b.tp.update_issue(num, labels=labels)
        log(f"[feedback] {entry['repo']}#{num} : retour poste (machine:{action})")
        return True
    except Exception as e:
        log(f"[feedback] echec {entry['repo']}#{num} : {e}")
        return False


def run(infra_path=None, token=None, ctx=None, feedback=False, log=print):
    """Boucle complete : ingestion (projects) -> dispatch -> (option) retro-action
    sur le board. Itere le registre, garde l'entree+board en portee pour le retour.
    Retourne la liste des decisions."""
    if not HANDLERS:
        _load_modes(log)
    import projects
    results = []
    for entry in projects.registry(infra_path):
        b = projects.board_for(entry, token=token)
        for art in projects.list_artifacts(entry, token=token, board=b, log=log):
            dec = dispatch_one(art, ctx, log)
            if feedback and b is not None:
                post_feedback(entry, dec, board=b, log=log)
            results.append(dec)
    return results


def dispatch_all(infra_path=None, token=None, ctx=None, log=print):
    """Compat : run() sans retro-action."""
    return run(infra_path, token, ctx, feedback=False, log=log)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Dispatch des artefacts GitOps.")
    ap.add_argument("--feedback", action="store_true",
                    help="reposter la decision sur le board (boucle de retro-action)")
    ap.add_argument("--apply", action="store_true",
                    help="executer reellement les actions (ex build kernel) au lieu "
                         "du dry-run")
    a = ap.parse_args()
    decs = run(ctx={"apply": a.apply}, feedback=a.feedback)
    print(json.dumps(decs, indent=2, ensure_ascii=False))

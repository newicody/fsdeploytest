#!/usr/bin/env python3
"""mode_kernel.py -- handler du MODE projet 'kernel' (s'enregistre dans dispatch).

Consomme les artefacts type:kernel-validation : le state: (label sur l'Issue)
encode l'AUTORISATION humaine (validation DECLARATIVE sur GitHub) et pilote
build/promote en DELEGUANT a kernel_build (jamais reimplemente ici). C'est le
reconciliateur recentre : l'etat desire = le state: de l'artefact, lu via l'API.

Sur par defaut : decision en DRY-RUN ; le build n'est declenche que si
ctx['apply'] est vrai. Mapping state -> action :
    prod  -> AUTORISE -> build vers la cible
    drop  -> ABANDON
    autre / vide -> EN ATTENTE (validation humaine requise)
"""
import os
import re
import subprocess
import sys

import dispatch


def _target_from(artifact):
    """Cible noyau : label 'kernel:<ver>' > version dans le titre > 'latest'."""
    for l in artifact.get("labels", []):
        if l.startswith("kernel:"):
            return l[len("kernel:"):].strip()
    m = re.search(r"\b\d+\.\d+(?:\.\d+)?\b", artifact.get("title", ""))
    return m.group(0) if m else "latest"


def _trigger_build(target, log):
    """Delegue a kernel_build (voie Gentoo existante). True si succes. On passe la
    cible en KVER_EXPECT : kernel_build REFUSE si le source ne produit pas cette
    version -> une validation GitOps "noyau X" ne peut jamais armer un autre noyau.
    'latest'/vide -> pas de contrainte (build du source courant)."""
    here = os.path.dirname(os.path.abspath(__file__))
    log(f"[kernel] declenchement build -> {target} via kernel_build")
    env = dict(os.environ)
    if target and target != "latest":
        env["KVER_EXPECT"] = target
    try:
        r = subprocess.run([sys.executable, os.path.join(here, "kernel_build.py")],
                           env=env)
        return r.returncode == 0
    except OSError as e:
        log(f"[kernel] echec declenchement kernel_build : {e}")
        return False


@dispatch.register("kernel")
def handle(artifact, ctx=None, log=print):
    """type:kernel-validation + state: -> decision declarative. Autre type -> skip."""
    if artifact.get("type") != "kernel-validation":
        return {"action": "skip", "reason": "type != kernel-validation",
                "number": artifact.get("number")}
    state = artifact.get("state", "")
    target = _target_from(artifact)
    if state == "prod":
        d = {"action": "build", "target": target, "number": artifact.get("number"),
             "reason": "state:prod -> autorise"}
        if ctx and ctx.get("apply"):
            d["applied"] = _trigger_build(target, log)
        else:
            log(f"[kernel] AUTORISE build {target} (dry-run ; ctx['apply']=True "
                "pour executer)")
        return d
    if state == "drop":
        return {"action": "abandon", "target": target,
                "number": artifact.get("number"), "reason": "state:drop"}
    return {"action": "wait", "target": target, "number": artifact.get("number"),
            "reason": f"state '{state or '(vide)'}' -> attente validation humaine"}

#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
dispatch_service.py -- boucle d'inference AUTONOME (PAS un point d'entree CLI).

L'inference n'est pas une commande : c'est un service de fond, pilote UNIQUEMENT
par infra.conf. session_launch.py le lance APRES le socle (reseau + token + logs
prets), en arriere-plan ; il ne bloque JAMAIS PID 1. A cadence reglee il interroge
le board, route via dispatch.run (ingestion projects -> dispatch -> arbiter quand
'needs:inference') et reposte la decision (feedback -> label machine:<action>).
Tout passe par l'existant : AUCUNE infra parallele, aucune logique metier ici.

Section infra.conf :
    [dispatch]
        enabled  = false   # MAITRE : false -> service inactif (defaut ; sur en chroot)
        interval = 900      # secondes entre deux passes (defaut 900 = 15 min)
        feedback = true     # reposter la decision sur l'artefact (machine:<action>)
        apply    = false    # false = dry-run (decision seule) ; true = execute (build...)
        jitter   = 60       # alea +/- (s) : ne pas marteler l'API a heure fixe

Lancement (par session_launch, jamais par operate) :
    python3 dispatch_service.py          # boucle, best-effort, ne rend pas la main
Passe unique (debug explicite, hors boucle) :
    python3 dispatch_service.py --once

Robuste : une passe en echec est journalisee et n'arrete pas le service. Le modele
d'inference est gere par [arbiter] (worker local OpenVINO) ; ici on ne fait QUE
cadencer et declencher. enabled=false (defaut) -> serve() rend la main aussitot.
"""
import os
import random
import sys
import time


# --------------------------------------------------------------------------- #
# config (la seule maniere de piloter le service)
# --------------------------------------------------------------------------- #
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


def _cfg(infra_path=None):
    cfg = _infra(infra_path) or {}
    d = (cfg.get("dispatch", {}) or {})

    def _b(v, default=False):
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on", "enabled")

    def _i(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return {
        "enabled":  _b(d.get("enabled"), False),
        "interval": max(30, _i(d.get("interval"), 900)),
        "feedback": _b(d.get("feedback"), True),
        "apply":    _b(d.get("apply"), False),
        "jitter":   max(0, _i(d.get("jitter"), 60)),
    }


def _log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[dispatch-service {ts}] {msg}", flush=True)


def _load_token():
    """Charge GITHUB_TOKEN depuis [manager] token_file s'il est absent (le service
    peut etre lance hors de l'env de session_launch). Best-effort ; n'ecrase pas
    un token deja present. Sans token, le board est injoignable -> passes a vide
    (non bloquant)."""
    if os.environ.get("GITHUB_TOKEN"):
        return
    cfg = _infra() or {}
    m = (cfg.get("manager", {}) or {})
    cands = []
    if m.get("token_file"):
        cands.append(m["token_file"])
    if m.get("root"):
        cands.append(os.path.join(m["root"], "github.token"))
    cands += ["/etc/github.token", "/boot_pool/manager/github.token"]
    for f in cands:
        try:
            if f and os.path.isfile(f):
                tok = open(f).read().strip()
                if tok:
                    os.environ["GITHUB_TOKEN"] = tok
                    return
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# une passe + boucle
# --------------------------------------------------------------------------- #
def run_once(cfg=None, log=_log):
    """Une passe : ingestion + dispatch (+ feedback). Retourne le nb d'artefacts
    traites. Best-effort : ne leve jamais."""
    cfg = cfg or _cfg()
    try:
        import dispatch
    except Exception as e:
        log(f"dispatch indisponible ({e}) -> passe sautee")
        return 0
    try:
        decs = dispatch.run(ctx={"apply": cfg["apply"]},
                            feedback=cfg["feedback"], log=log)
        acted = sum(1 for d in decs
                    if d.get("action") not in (None, "none", "skip", "wait"))
        log(f"passe terminee : {len(decs)} artefact(s), {acted} action(s)")
        return len(decs)
    except Exception as e:
        log(f"passe en echec ({e}) -> on continue")
        return 0


def serve(infra_path=None, log=_log):
    """Boucle autonome pilotee par [dispatch] d'infra.conf. Ne rend pas la main
    tant que enabled=true (relue a chaque tour : l'operateur peut couper sans
    reboot). enabled=false -> retourne aussitot (sur en chroot / automatisation
    coupee)."""
    cfg = _cfg(infra_path)
    if not cfg["enabled"]:
        log("[dispatch] enabled=false -> service inactif (rien a faire).")
        return 0
    _load_token()
    if not os.environ.get("GITHUB_TOKEN"):
        log("aucun GITHUB_TOKEN -> board injoignable ; les passes seront a vide.")
    log(f"demarrage : interval={cfg['interval']}s feedback={cfg['feedback']} "
        f"apply={cfg['apply']} jitter={cfg['jitter']}s")
    while True:
        cfg = _cfg(infra_path)                 # relire : coupure a chaud possible
        if not cfg["enabled"]:
            log("enabled repasse a false -> arret de la boucle.")
            return 0
        run_once(cfg, log)
        delay = cfg["interval"]
        if cfg["jitter"]:
            delay += random.randint(-cfg["jitter"], cfg["jitter"])
        time.sleep(max(30, delay))


def main():
    if "--once" in sys.argv[1:]:
        # passe unique meme si enabled=false (debug explicite, pas de boucle)
        run_once(_cfg())
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main() or 0)

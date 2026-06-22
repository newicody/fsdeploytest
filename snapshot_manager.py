#!/usr/bin/env python3
"""snapshot_manager.py -- gestionnaire central des snapshots ZFS.

Vue d'ensemble de TOUS les snapshots, classes par TYPE (prefixe apres '@') :
  auto-    : crees par cet outil (manuels/planifies)
  freeze-  : crees par freeze_overlay.py (avant de figer un rootfs)
  presfs-  : crees par init.py (avant reset d'un upper perime)
  autre    : tout autre prefixe

Politique de retention dans [snapshots] d'infra.conf, PAR DATASET et PAR TYPE :
la rotation garde les 'keep' plus recents de chaque (dataset, type) et purge le
reste. Les types sont traites separement pour ne pas melanger les usages (ex.
garder 10 'freeze' ET 10 'auto' sur fast_pool/rootfs).

Commandes :
  snapshot_manager.py list [dataset]        # tous (ou d'un dataset), par type
  snapshot_manager.py create <dataset>      # snapshot 'auto-<timestamp>'
  snapshot_manager.py rotate [--apply]      # applique la retention (dry par defaut)
  snapshot_manager.py purge <dataset@snap>  # supprime un snapshot precis
  snapshot_manager.py rollback <dataset@snap>  # DESTRUCTIF : revient au snapshot

ASCII-only, stdlib + zfs + configobj. Rotation 'dry-run' par defaut (sur).
"""
import os
import sys
import time
import subprocess


def log(m):
    print(m, flush=True)


def _capture(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout.strip()
    except OSError as e:
        return 1, str(e)


def _run(cmd):
    log("$ " + " ".join(cmd))
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        log(f"  erreur: {e}")
        return 1


KNOWN_TYPES = ("auto", "freeze", "presfs")


def _snap_type(snapname):
    """Type d'un snapshot d'apres le prefixe de sa partie apres '@'.
    'auto-20260101-..' -> 'auto' ; 'freeze-..' -> 'freeze' ; sinon 'autre'."""
    short = snapname.split("@", 1)[1] if "@" in snapname else snapname
    for t in KNOWN_TYPES:
        if short.startswith(t + "-") or short == t:
            return t
    return "autre"


def _all_snapshots(dataset=None):
    """Liste (name, creation_epoch) des snapshots, tries du plus ancien au recent.
    Si dataset fourni, limite a ce dataset."""
    cmd = ["zfs", "list", "-H", "-t", "snapshot", "-o", "name,creation",
           "-p", "-s", "creation"]
    if dataset:
        cmd += ["-r", dataset]
    rc, out = _capture(cmd)
    if rc != 0 or not out:
        return []
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                res.append((parts[0], int(parts[1])))
            except ValueError:
                res.append((parts[0], 0))
    # si dataset fourni, ne garder QUE ce dataset (pas les enfants)
    if dataset:
        res = [(n, c) for (n, c) in res if n.split("@", 1)[0] == dataset]
    return res


def cmd_list(dataset=None):
    snaps = _all_snapshots(dataset)
    if not snaps:
        log("aucun snapshot" + (f" pour {dataset}" if dataset else ""))
        return
    # grouper par dataset puis par type
    by_ds = {}
    for name, cr in snaps:
        ds = name.split("@", 1)[0]
        by_ds.setdefault(ds, []).append((name, cr))
    for ds in sorted(by_ds):
        log(f"\n{ds} :")
        by_type = {}
        for name, cr in by_ds[ds]:
            by_type.setdefault(_snap_type(name), []).append((name, cr))
        for t in sorted(by_type):
            items = by_type[t]
            log(f"  [{t}] ({len(items)})")
            for name, cr in items:
                short = name.split("@", 1)[1]
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(cr)) \
                    if cr else "?"
                log(f"      {short:<32} {when}")


def cmd_create(dataset, prefix="auto"):
    if "@" in dataset:
        sys.exit("donne un DATASET (sans @snap) a 'create'.")
    rc, _ = _capture(["zfs", "list", "-H", "-o", "name", dataset])
    if rc != 0:
        sys.exit(f"dataset introuvable : {dataset}")
    ts = time.strftime("%Y%m%d-%H%M%S")
    snap = f"{dataset}@{prefix}-{ts}"
    if _run(["zfs", "snapshot", snap]) != 0:
        sys.exit(f"echec creation snapshot {snap}")
    log(f"snapshot cree : {snap}")


def _load_policy(infra_path):
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(infra_path)
        return cfg.get("snapshots", {})
    except Exception as e:
        log(f"[!] infra.conf illisible ({e})")
        return {}


def cmd_rotate(infra_path, apply=False):
    """Applique la retention [snapshots] : pour chaque (dataset, type), garder les
    'keep' plus recents, purger le reste. Dry-run par defaut (affiche seulement)."""
    pol = _load_policy(infra_path)
    if not pol:
        sys.exit("aucune politique [snapshots] dans infra.conf")
    auto_prefix = pol.get("auto_prefix", "auto")
    mode = "APPLIQUE" if apply else "DRY-RUN (rien supprime ; --apply pour agir)"
    log("=" * 60)
    log(f"ROTATION DES SNAPSHOTS -- {mode}")
    log("=" * 60)
    total_purge = 0
    for ds, spec in pol.items():
        if ds == "auto_prefix" or not isinstance(spec, dict):
            continue
        try:
            keep = int(spec.get("keep", 0))
        except (TypeError, ValueError):
            keep = 0
        if keep <= 0:
            continue
        snaps = _all_snapshots(ds)
        if not snaps:
            continue
        # grouper par type, garder les 'keep' plus recents de CHAQUE type
        by_type = {}
        for name, cr in snaps:
            by_type.setdefault(_snap_type(name), []).append((name, cr))
        for t, items in by_type.items():
            items.sort(key=lambda x: x[1])          # ancien -> recent
            excess = items[:-keep] if keep < len(items) else []
            if not excess:
                continue
            log(f"\n{ds} [{t}] : {len(items)} snapshots, keep={keep} "
                f"-> purge {len(excess)}")
            for name, _cr in excess:
                total_purge += 1
                if apply:
                    _run(["zfs", "destroy", name])
                else:
                    log(f"      (dry) destroy {name}")
    log("=" * 60)
    log(f"BILAN : {total_purge} snapshot(s) "
        + ("purge(s)." if apply else "a purger (relance avec --apply)."))


def cmd_purge(target):
    if "@" not in target:
        sys.exit("donne un SNAPSHOT precis (dataset@snap) a 'purge'.")
    rc, _ = _capture(["zfs", "list", "-H", "-t", "snapshot", "-o", "name", target])
    if rc != 0:
        sys.exit(f"snapshot introuvable : {target}")
    if _run(["zfs", "destroy", target]) != 0:
        sys.exit(f"echec destroy {target}")
    log(f"snapshot supprime : {target}")


def cmd_rollback(target, force=False):
    """DESTRUCTIF : ramene le dataset a l'etat du snapshot. Tout ce qui a ete
    ecrit APRES est PERDU. zfs rollback refuse si des snapshots plus recents
    existent, sauf -r (qui les detruit) -> demande confirmation explicite."""
    if "@" not in target:
        sys.exit("donne un SNAPSHOT precis (dataset@snap) a 'rollback'.")
    rc, _ = _capture(["zfs", "list", "-H", "-t", "snapshot", "-o", "name", target])
    if rc != 0:
        sys.exit(f"snapshot introuvable : {target}")
    ds = target.split("@", 1)[0]
    # snapshots plus recents que la cible ?
    snaps = _all_snapshots(ds)
    names = [n for n, _ in snaps]
    newer = names[names.index(target) + 1:] if target in names else []
    log("=" * 60)
    log(f"ROLLBACK DESTRUCTIF de {ds} vers {target}")
    log("  -> tout ce qui a ete ecrit APRES ce snapshot sera PERDU.")
    if newer:
        log(f"  -> {len(newer)} snapshot(s) plus recent(s) seront DETRUITS :")
        for n in newer:
            log(f"        {n.split('@',1)[1]}")
    log("=" * 60)
    if not force:
        try:
            rep = input("Taper 'ROLLBACK' pour confirmer : ").strip()
        except EOFError:
            rep = ""
        if rep != "ROLLBACK":
            sys.exit("annule.")
    cmd = ["zfs", "rollback"]
    if newer:
        cmd.append("-r")        # detruit les snapshots plus recents (obligatoire)
    cmd.append(target)
    if _run(cmd) != 0:
        sys.exit("echec du rollback.")
    log(f"rollback effectue : {ds} est revenu a {target}")
    log("  (si c'est un dataset monte/en usage, un reboot peut etre necessaire.)")


def _find_infra():
    for c in ("/etc/infra.conf", "infra.conf", "/infra.conf"):
        if os.path.exists(c):
            return c
    return "/etc/infra.conf"


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Gestionnaire central de snapshots ZFS.")
    ap.add_argument("command",
                    choices=["list", "create", "rotate", "purge", "rollback"])
    ap.add_argument("target", nargs="?", help="dataset ou dataset@snap")
    ap.add_argument("--apply", action="store_true",
                    help="rotate : applique reellement (sinon dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="rollback : sans confirmation interactive")
    ap.add_argument("--infra", default=None, help="chemin d'infra.conf")
    a = ap.parse_args()

    if a.command in ("create", "rotate", "purge", "rollback") and os.geteuid() != 0:
        sys.exit("cette commande necessite root.")
    infra = a.infra or _find_infra()

    if a.command == "list":
        cmd_list(a.target)
    elif a.command == "create":
        if not a.target:
            sys.exit("usage : create <dataset>")
        pol = _load_policy(infra)
        cmd_create(a.target, pol.get("auto_prefix", "auto"))
    elif a.command == "rotate":
        cmd_rotate(infra, apply=a.apply)
    elif a.command == "purge":
        if not a.target:
            sys.exit("usage : purge <dataset@snap>")
        cmd_purge(a.target)
    elif a.command == "rollback":
        if not a.target:
            sys.exit("usage : rollback <dataset@snap>")
        cmd_rollback(a.target, force=a.force)


if __name__ == "__main__":
    main()

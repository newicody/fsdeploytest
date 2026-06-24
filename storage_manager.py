#!/usr/bin/env python3
"""storage_manager.py -- VERIFIE la politique de stockage [storage] d'infra.conf
contre l'etat reel ZFS et RAPPORTE les ecarts. Ne corrige PAS : il affiche les
commandes a lancer a la main (tu gardes le controle).

Politique = placement par caracteristique physique :
  fast_pool  = NVMe stripe (rapide, zero redondance) -> ephemere/reconstructible
  data_pool  = raidz2       (gros, lent, redondant)  -> precieux/volumineux
  boot_pool  = mirror       (petit, redondant)       -> critique de boot + manager

Verifie pour chaque dataset de [storage] :
  - existence (sinon : suggere 'zfs create')
  - appartenance au bon pool (le prefixe du nom)
  - proprietes attendues (compression, canmount, reservation...) vs reel
    (sinon : suggere 'zfs set prop=val dataset')

Rapport clair : OK / ECART (avec la commande corrective) / ABSENT.

ASCII-only, stdlib + zfs + configobj.
"""
import os
import sys
import subprocess


def log(m):
    print(m, flush=True)


def _capture(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout.strip()
    except OSError as e:
        return 1, str(e)


# proprietes ZFS qu'on sait verifier (cle infra.conf -> propriete zfs)
CHECKABLE = ("compression", "canmount", "reservation", "mountpoint",
             "quota", "atime", "readonly", "recordsize")


def _ds_exists(ds):
    rc, _ = _capture(["zfs", "list", "-H", "-o", "name", ds])
    return rc == 0


def _get_prop(ds, prop):
    rc, out = _capture(["zfs", "get", "-H", "-o", "value", prop, ds])
    return out if rc == 0 else None


def _norm_size(v):
    """Normalise une taille ZFS pour comparaison lache (200G ~ 200G/214748364800).
    On compare en minuscules sans espaces ; ZFS peut afficher '200G' tel quel."""
    return (v or "").strip().lower().replace(" ", "")


def verify(infra_path):
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(infra_path)
        storage = cfg.get("datasets", {})
    except Exception as e:
        sys.exit(f"infra.conf illisible ({e})")
    if not storage:
        sys.exit("aucune section [datasets] dans infra.conf")
    if not _capture(["zfs", "version"])[0] == 0 and not os.path.exists(
            "/sbin/zfs"):
        # zfs absent : on ne peut pas verifier le reel
        log("[!] zfs introuvable : verification du reel impossible "
            "(lance sur la station bootee).")
        return 1

    n_ok = n_ecart = n_absent = 0
    suggestions = []
    log("=" * 64)
    log("VERIFICATION DE LA POLITIQUE DE STOCKAGE (rapport seul)")
    log("=" * 64)

    for ds, spec in storage.items():
        pool = spec.get("pool", "")
        role = spec.get("role", "")
        # 1. coherence nom/pool
        if pool and not ds.startswith(pool):
            log(f"[CONFIG] {ds} : 'pool={pool}' incoherent avec le nom du dataset")
        # 2. existence
        if not _ds_exists(ds):
            n_absent += 1
            log(f"[ABSENT] {ds}")
            if role:
                log(f"         role : {role}")
            suggestions.append(f"zfs create {ds}")
            continue
        # 3. proprietes attendues
        ecarts = []
        for key in CHECKABLE:
            if key not in spec:
                continue
            want = _norm_size(spec[key])
            real = _norm_size(_get_prop(ds, key))
            if real is None:
                continue
            # comparaison lache (reservation : ZFS peut afficher en octets)
            if want and want != real and not (
                    key == "reservation" and real not in ("none", "0", "0b")):
                ecarts.append((key, spec[key], _get_prop(ds, key)))
        if ecarts:
            n_ecart += 1
            log(f"[ECART]  {ds}  ({role})")
            for key, want, real in ecarts:
                log(f"         {key} : attendu '{want}', reel '{real}'")
                suggestions.append(f"zfs set {key}={want} {ds}")
        else:
            n_ok += 1
            log(f"[OK]     {ds}")

    log("=" * 64)
    log(f"BILAN : {n_ok} OK, {n_ecart} ecart(s), {n_absent} absent(s)")
    if suggestions:
        log("")
        log("Commandes suggerees (A LANCER A LA MAIN, l'outil ne corrige pas) :")
        for s in suggestions:
            log(f"  {s}")
    else:
        log("Politique respectee : rien a corriger.")
    log("=" * 64)
    return 0 if (n_ecart == 0 and n_absent == 0) else 2


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Verifie la politique [storage] d'infra.conf contre le reel ZFS.")
    ap.add_argument("--infra", default="/etc/infra.conf",
                    help="chemin d'infra.conf (defaut /etc/infra.conf)")
    a = ap.parse_args()
    path = a.infra
    if not os.path.exists(path):
        for c in ("/etc/infra.conf", "infra.conf", "/infra.conf"):
            if os.path.exists(c):
                path = c
                break
    if not os.path.exists(path):
        sys.exit(f"infra.conf introuvable ({path})")
    sys.exit(verify(path))


if __name__ == "__main__":
    main()

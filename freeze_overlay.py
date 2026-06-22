#!/usr/bin/env python3
"""freeze_overlay.py -- fige l'overlay racine COURANT en un nouveau rootfs.sfs
versionne, A CHAUD, depuis la station bootee (pas le chroot).

Principe (approche "overlay offline", sans toucher au systeme vivant) :
  1. snapshot ZFS de l'upper (fast_pool/rootfs@freeze-<ts>) : etat fige + securite.
  2. monter un overlay OFFLINE dans un staging :
       lower = rootfs.sfs actuel (monte ro)
       upper = clone du snapshot de l'upper (etat coherent, fige)
       merged = ce que verra mksquashfs (whiteouts overlayfs deja appliques).
  3. mksquashfs du merged -> rootfs-vN.sfs (VERSIONNE, n'ecrase pas l'actuel).
  4. demontage propre de l'overlay offline + du clone.
  5. le nouveau sfs est pret ; au prochain boot, pointer dessus (l'upper se reset
     car le CRC du sfs change -> mecanisme upper_stale de init.py).

Pourquoi l'approche offline : on ne mksquashfs JAMAIS '/' (systeme vivant,
fichiers ouverts, pseudo-FS). On remonte un overlay identique a partir d'un
snapshot fige -> image coherente, reproductible, sans risque pour la session.

ASCII-only, stdlib + outils systeme (zfs, mount, mksquashfs). configobj pour lire
infra.conf (chemins/datasets). Reutilise sfs_build._mksquashfs (pas de doublon).
"""
import os
import sys
import time
import subprocess
import shutil


def log(m):
    print(f"[freeze] {m}", flush=True)


def _run(cmd, **kw):
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, **kw)


def _capture(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout.strip()
    except OSError as e:
        return 1, str(e)


# --- parametres (alignes sur init.py / infra.conf) ---------------------------
POOL = os.environ.get("FREEZE_POOL", "fast_pool")
UPPER_DS = os.environ.get("FREEZE_UPPER_DS", f"{POOL}/rootfs")
SFS_DS = os.environ.get("FREEZE_SFS_DS", f"{POOL}/sfs")
ROOTFS_SFS = "rootfs.sfs"


def _ds_mountpoint(ds):
    rc, out = _capture(["zfs", "get", "-H", "-o", "value", "mountpoint", ds])
    return out if rc == 0 else ""


def _sfs_dir():
    """Repertoire contenant rootfs.sfs (le mountpoint du dataset sfs)."""
    mp = _ds_mountpoint(SFS_DS)
    if mp and mp not in ("legacy", "none") and os.path.isdir(mp):
        return mp
    # repli : chemins usuels
    for c in (f"/{SFS_DS}", "/mnt/sfs"):
        if os.path.isdir(c):
            return c
    return ""


def _next_version(sfs_dir):
    """Determine le prochain numero de version : rootfs-vN.sfs."""
    n = 0
    try:
        for f in os.listdir(sfs_dir):
            if f.startswith("rootfs-v") and f.endswith(".sfs"):
                try:
                    n = max(n, int(f[len("rootfs-v"):-len(".sfs")]))
                except ValueError:
                    pass
    except OSError:
        pass
    return n + 1


def freeze(force=False):
    """Fige l'overlay courant en rootfs-v<N>.sfs. Retourne le chemin ou None."""
    # prerequis
    for tool in ("zfs", "mount", "umount", "mksquashfs"):
        if not shutil.which(tool):
            log(f"ERREUR : '{tool}' introuvable. Abandon.")
            return None

    sfs_dir = _sfs_dir()
    if not sfs_dir:
        log(f"ERREUR : dataset sfs ({SFS_DS}) non monte / introuvable.")
        return None
    lower_sfs = os.path.join(sfs_dir, ROOTFS_SFS)
    if not os.path.isfile(lower_sfs):
        log(f"ERREUR : {lower_sfs} (lower actuel) introuvable.")
        return None

    upper_mp = _ds_mountpoint(UPPER_DS)
    if not upper_mp or upper_mp in ("legacy", "none"):
        log(f"ERREUR : upper {UPPER_DS} sans mountpoint exploitable.")
        return None

    # garantir canmount=noauto sur l'upper : il ne doit jamais etre monte
    # automatiquement (reserve a l'overlay). On travaille sur un CLONE de toute
    # facon, donc l'original n'est pas touche, mais on fige la bonne politique.
    _run(["zfs", "set", "canmount=noauto", UPPER_DS], stderr=subprocess.DEVNULL)

    ts = time.strftime("%Y%m%d-%H%M%S")
    snap = f"{UPPER_DS}@freeze-{ts}"
    clone = f"{POOL}/freeze-clone-{ts}"
    work = f"/tmp/freeze-{ts}"
    d_lower = f"{work}/lower"      # rootfs.sfs monte ro
    d_upper = f"{work}/upper"      # clone du snapshot (l'upper fige)
    d_merged = f"{work}/merged"    # overlay offline
    d_work = f"{work}/work"        # workdir overlayfs (meme FS que upper)
    mounts = []                    # pour le nettoyage en ordre inverse

    def cleanup():
        for m in reversed(mounts):
            _run(["umount", m], stderr=subprocess.DEVNULL)
        # detruire le clone (le SNAPSHOT, lui, est conserve : securite/rollback)
        _run(["zfs", "destroy", "-r", clone], stderr=subprocess.DEVNULL)
        try:
            shutil.rmtree(work, ignore_errors=True)
        except OSError:
            pass

    try:
        # 1. SNAPSHOT de l'upper (securite + etat fige coherent)
        if _run(["zfs", "snapshot", snap]).returncode != 0:
            log(f"ERREUR : snapshot {snap} echoue.")
            return None
        log(f"snapshot cree : {snap} (conserve pour rollback)")

        # 2. CLONE du snapshot -> upper fige, montable en RW pour overlayfs
        #    (overlayfs exige un upperdir inscriptible ; le clone l'est).
        #    -o canmount=on + mountpoint dedie : le clone herite de noauto de
        #    l'origine, on force donc son montage a un emplacement propre.
        clone_mp = f"{work}/clone"
        os.makedirs(clone_mp, exist_ok=True)
        if _run(["zfs", "clone", "-o", "canmount=noauto",
                 "-o", f"mountpoint={clone_mp}", snap, clone]).returncode != 0:
            log(f"ERREUR : clone {clone} echoue.")
            cleanup()
            return None
        # montage explicite du clone a son mountpoint dedie
        if _run(["zfs", "mount", clone]).returncode != 0:
            # repli : mount.zfs direct avec zfsutil
            _run(["mount.zfs", "-o", "zfsutil", clone, clone_mp],
                 stderr=subprocess.DEVNULL)
        if not os.path.ismount(clone_mp):
            log(f"ERREUR : clone non monte sur {clone_mp}.")
            cleanup()
            return None
        mounts.append(clone_mp)

        # 3. preparer les points de montage de l'overlay OFFLINE
        for d in (d_lower, d_upper, d_merged, d_work):
            os.makedirs(d, exist_ok=True)

        # lower = rootfs.sfs actuel, monte READ-ONLY (loop squashfs)
        if _run(["mount", "-t", "squashfs", "-o", "ro,loop",
                 lower_sfs, d_lower]).returncode != 0:
            log("ERREUR : montage du lower (rootfs.sfs) echoue.")
            cleanup()
            return None
        mounts.append(d_lower)

        # upper = le clone (contenu 'upper' de l'overlay live, fige). L'upper
        # overlayfs reel est dans clone_mp/upper (init.py utilise /mnt/ovl/upper).
        real_upper = os.path.join(clone_mp, "upper")
        if not os.path.isdir(real_upper):
            # certains layouts mettent l'upper a la racine du dataset
            real_upper = clone_mp
        real_work = os.path.join(clone_mp, "work")
        os.makedirs(real_work, exist_ok=True)

        # 4. overlay OFFLINE : merged = lower(sfs) + upper(clone). Les whiteouts
        #    sont appliques par overlayfs -> merged = etat EXACT du systeme.
        #    index=off : l'upper (clone) porte un xattr 'origin' herite de
        #    l'overlay live ; on desactive la verification d'origine (meme raison
        #    que init.py) pour ne jamais echouer en ESTALE si le file handle du
        #    lower (squashfs reboucle) differe de celui enregistre.
        ov_opts = (f"index=off,lowerdir={d_lower},upperdir={real_upper},"
                   f"workdir={real_work}")
        if _run(["mount", "-t", "overlay", "overlay", "-o", ov_opts,
                 d_merged]).returncode != 0:
            log("ERREUR : montage de l'overlay offline echoue.")
            cleanup()
            return None
        mounts.append(d_merged)
        log(f"overlay offline monte : {d_merged} (lower=sfs + upper=clone)")

        # 5. mksquashfs du merged -> rootfs-vN.sfs (VERSIONNE)
        version = _next_version(sfs_dir)
        out_name = f"rootfs-v{version}.sfs"
        out_path = os.path.join(sfs_dir, out_name)
        log(f"compression du merged -> {out_path} (peut etre long)...")

        # reutiliser _mksquashfs de sfs_build (exclusions + ecriture atomique)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sfs_build
        extra = ["-e"] + sfs_build.ROOTFS_EXCLUDES
        res = sfs_build._mksquashfs(d_merged, out_path, sfs_dir, log, extra=extra)
        if not res.ok:
            log(f"ERREUR mksquashfs : {res.reason}")
            cleanup()
            return None
        log(f"nouveau rootfs fige : {out_path} ({res.size_mb} Mo)")
        log(f"  snapshot upper conserve : {snap}")
        log(f"  pour booter dessus : pointe rootfs.sfs vers {out_name} "
            f"(ou mets a jour la cmdline/lien), puis reboot.")
        log(f"  l'upper se reinitialisera au boot (CRC du sfs different).")
        return out_path

    finally:
        cleanup()
        log("nettoyage termine (clone detruit, overlay demonte, snapshot garde).")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Fige l'overlay racine courant en nouveau rootfs.sfs versionne.")
    ap.add_argument("--force", action="store_true",
                    help="ne pas demander confirmation")
    a = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("freeze_overlay doit etre lance en root.")

    log("Fige l'overlay COURANT (lower sfs + tes modifs upper) en nouveau sfs.")
    log(f"  upper : {UPPER_DS}   sfs : {SFS_DS}")
    if not a.force:
        try:
            rep = input("Continuer ? [o/N] ").strip().lower()
        except EOFError:
            rep = ""
        if rep not in ("o", "oui", "y", "yes"):
            sys.exit("annule.")

    path = freeze(force=a.force)
    if not path:
        sys.exit("echec du fige de l'overlay.")
    log(f"OK : {path}")


if __name__ == "__main__":
    main()

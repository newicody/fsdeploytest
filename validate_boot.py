#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
validate_boot.py — valider que les artefacts de boot "passeront les tests".

Deux familles de verification, AVANT de compter sur un artefact :

  - SFS (rootfs.sfs, modules-<ver>.sfs) : un fichier squashfs peut exister mais
    etre tronque/corrompu. On le MONTE en lecture seule (loop) et on verifie
    qu'il s'ouvre ET contient le contenu attendu (rootfs : /sbin,/etc,... ;
    modules : l'arbre de modules). Demonte proprement apres.

  - ESP (partition EFI) : doit etre vfat, accessible (montee ou montable) et
    avoir assez de place pour vmlinuz + initramfs + UKI.

Retour observable (Check), pas d'exception qui fuit. ASCII-only, stdlib +
outils systeme (mount, blkid, losetup via mount -o loop).
"""
import os
import subprocess
import tempfile

try:
    from common import sh as _csh
    def _sh(cmd):
        return _csh(cmd)
except ImportError:
    def _sh(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except OSError as e:
            return 1, "", str(e)


class Check:
    __slots__ = ("ok", "what", "reason", "details")

    def __init__(self, ok, what, reason="", details=None):
        self.ok = bool(ok)
        self.what = what
        self.reason = reason
        self.details = details or {}

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"<Check {self.what} {'OK' if self.ok else 'FAIL'} {self.reason}>"


# --------------------------------------------------------------------------- #
# validation SFS : montage RO test + contenu attendu
# --------------------------------------------------------------------------- #
# contenu minimal attendu selon le type de SFS
EXPECT_ROOTFS = ["sbin", "etc", "usr", "lib"]      # un rootfs a au moins ca
EXPECT_MODULES = ["kernel"]                        # /lib/modules/<ver>/kernel/...


def validate_sfs(path, kind="rootfs", log=print):
    """Monte le squashfs en lecture seule et verifie son contenu. kind =
    'rootfs' | 'modules' | 'any'. Retourne Check."""
    if not os.path.exists(path):
        return Check(False, f"sfs:{os.path.basename(path)}", "fichier absent")
    size = os.path.getsize(path)
    if size < 4096:
        return Check(False, f"sfs:{os.path.basename(path)}",
                     f"trop petit ({size} o) -- creation incomplete ?")
    # signature squashfs : magic 'hsqs' (little-endian) en debut de fichier
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        if magic != b"hsqs":
            return Check(False, f"sfs:{os.path.basename(path)}",
                         f"signature squashfs absente (magic={magic!r})")
    except OSError as e:
        return Check(False, f"sfs:{os.path.basename(path)}", str(e))

    mnt = tempfile.mkdtemp(prefix="sfscheck_")
    mounted = False
    try:
        rc, _, err = _sh(["mount", "-o", "loop,ro", path, mnt])
        if rc != 0:
            return Check(False, f"sfs:{os.path.basename(path)}",
                         f"montage RO impossible (corrompu ?) : {err[:80]}")
        mounted = True
        present = set(os.listdir(mnt))
        expect = (EXPECT_ROOTFS if kind == "rootfs" else
                  EXPECT_MODULES if kind == "modules" else [])
        missing = [e for e in expect if e not in present]
        if missing:
            return Check(False, f"sfs:{os.path.basename(path)}",
                         f"monte mais contenu attendu absent : {missing} "
                         f"(present: {sorted(present)[:6]})")
        return Check(True, f"sfs:{os.path.basename(path)}",
                     f"montable + contenu OK ({size // (1024*1024)} Mo)",
                     {"size_mb": size // (1024 * 1024)})
    finally:
        if mounted:
            _sh(["umount", mnt])
        try:
            os.rmdir(mnt)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# validation ESP : vfat + accessible + place
# --------------------------------------------------------------------------- #
def validate_esp(partition, need_mb=200, log=print):
    """Verifie qu'une ESP est vfat, accessible (montee ou montable en test) et
    a assez de place. partition = /dev/nvme0n1p1. Retourne Check."""
    if not os.path.exists(partition):
        return Check(False, f"esp:{partition}", "partition absente")
    rc, fstype, _ = _sh(["blkid", "-o", "value", "-s", "TYPE", partition])
    if rc != 0 or "vfat" not in fstype.lower():
        return Check(False, f"esp:{partition}",
                     f"type {fstype or '?'} (vfat attendu)")
    # deja montee ?
    where = ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == partition:
                    where = p[1]
                    break
    except OSError:
        pass
    tmp_mounted = False
    mnt = where
    try:
        if not mnt:
            mnt = tempfile.mkdtemp(prefix="espcheck_")
            rc, _, err = _sh(["mount", partition, mnt])
            if rc != 0:
                return Check(False, f"esp:{partition}",
                             f"montage test impossible : {err[:80]}")
            tmp_mounted = True
        # place libre
        st = os.statvfs(mnt)
        free_mb = st.f_bavail * st.f_frsize / (1024 * 1024)
        if free_mb < need_mb:
            return Check(False, f"esp:{partition}",
                         f"place insuffisante : {free_mb:.0f} Mo "
                         f"(~{need_mb} requis)")
        return Check(True, f"esp:{partition}",
                     f"vfat, accessible, {free_mb:.0f} Mo libres",
                     {"free_mb": int(free_mb), "mount": mnt})
    finally:
        if tmp_mounted:
            _sh(["umount", mnt])
            try:
                os.rmdir(mnt)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# validation groupee (generateur) + CLI
# --------------------------------------------------------------------------- #
def validate_all(sfs_list=None, esp_list=None, need_mb=200, log=print):
    """Genere des Check pour tous les artefacts fournis.
       sfs_list : liste de (path, kind) ; esp_list : liste de partitions."""
    for path, kind in (sfs_list or []):
        c = validate_sfs(path, kind, log)
        log(f"  [{'OK ' if c.ok else '!! '}] {c.what}: {c.reason}")
        yield c
    for part in (esp_list or []):
        c = validate_esp(part, need_mb, log)
        log(f"  [{'OK ' if c.ok else '!! '}] {c.what}: {c.reason}")
        yield c


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="valide SFS + ESP avant de booter")
    ap.add_argument("--rootfs", help="chemin rootfs.sfs")
    ap.add_argument("--modules", help="chemin modules-<ver>.sfs")
    ap.add_argument("--esp", action="append", default=[], help="partition ESP (repetable)")
    a = ap.parse_args()
    sfs = []
    if a.rootfs:
        sfs.append((a.rootfs, "rootfs"))
    if a.modules:
        sfs.append((a.modules, "modules"))
    bad = 0
    print(">> validation des artefacts de boot")
    for c in validate_all(sfs_list=sfs, esp_list=a.esp):
        if not c.ok:
            bad += 1
    print(f"\n{'TOUT OK' if not bad else f'{bad} probleme(s)'} "
          "-- (root requis pour les montages test)")
    raise SystemExit(1 if bad else 0)

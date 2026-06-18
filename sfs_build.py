#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
sfs_build.py — creation des images squashfs (rootfs.sfs + modules-<ver>.sfs).

Module dedie reutilisable : appele par first_boot.py (rootfs) et kernel_build.py
(modules). Centralise la logique mksquashfs pour ne pas la dupliquer.

Choix de staging : /tmp (tmpfs en RAM) plutot que /var/tmp, car /var vient de
l'overlay -> ecrire des Go dans /var/tmp salit l'upper persistant. /tmp est
volatile (RAM), nettoye au reboot, et avec 128 Go de RAM un rootfs.sfs compresse
tient large. Repli sur /var/tmp UNIQUEMENT si /tmp est trop petit (avec alerte).

Retour : objets a attributs observables (.ok / .path / .reason), pas
d'exception qui fuit (coherent avec le reste du projet).
ASCII-only, stdlib + outils systeme (mksquashfs, zfs).
"""
import os
import shutil
import subprocess


class SfsResult:
    __slots__ = ("ok", "path", "reason", "size_mb")

    def __init__(self, ok, path="", reason="", size_mb=0):
        self.ok = bool(ok)
        self.path = path
        self.reason = reason
        self.size_mb = size_mb

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return (f"<SfsResult {'ok' if self.ok else 'FAIL'} {self.path} "
                f"{self.size_mb}MB {self.reason}>")


def _free_mb(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 * 1024)
    except OSError:
        return 0.0


def _pick_staging(estimate_mb, log):
    """Choisit le repertoire de staging : /tmp (tmpfs) si assez de place, sinon
    /var/tmp avec alerte. estimate_mb = taille presumee du .sfs final."""
    need = max(estimate_mb * 1.3, 512)         # marge 30 %, plancher 512 Mo
    tmp_free = _free_mb("/tmp")
    if tmp_free >= need:
        return "/tmp"
    log(f"/tmp insuffisant ({tmp_free:.0f} Mo libres, ~{need:.0f} requis) "
        f"-> repli /var/tmp (ATTENTION : salit l'overlay si persistant)")
    return "/var/tmp"


def _dir_size_mb(path):
    """Taille approximative d'une arborescence (pour estimer le .sfs)."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / (1024 * 1024)


def _mksquashfs(src, dst, staging, log, extra=None):
    """Compresse src -> dst via mksquashfs, en passant par un fichier temporaire
    dans staging (tmpfs), puis deplace vers dst. Nettoie le temporaire.
    Retourne SfsResult."""
    if not os.path.isdir(src):
        return SfsResult(False, dst, f"source absente : {src}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = os.path.join(staging, os.path.basename(dst) + ".new")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    cmd = ["mksquashfs", src, tmp, "-comp", "zstd", "-Xcompression-level", "19",
           "-xattrs", "-noappend", "-quiet", "-processors", str(os.cpu_count() or 1)]
    if extra:
        cmd += extra
    log(f"mksquashfs {src} -> {dst} (staging {staging})")
    try:
        p = subprocess.run(cmd)
    except FileNotFoundError:
        return SfsResult(False, dst, "mksquashfs introuvable "
                         "(installe sys-fs/squashfs-tools)")
    if p.returncode != 0:
        _safe_rm(tmp)
        return SfsResult(False, dst, f"mksquashfs a echoue (rc={p.returncode})")
    # controle : le .sfs temporaire existe et n'est pas vide
    try:
        size = os.path.getsize(tmp)
    except OSError:
        return SfsResult(False, dst, "fichier squashfs introuvable apres creation")
    if size < 4096:
        _safe_rm(tmp)
        return SfsResult(False, dst, f"squashfs anormalement petit ({size} o)")
    # deplacement atomique vers la destination finale (meme FS si possible)
    try:
        if _same_fs(staging, os.path.dirname(dst)):
            os.replace(tmp, dst)
        else:
            shutil.move(tmp, dst)              # staging tmpfs -> dataset : copie
    except OSError as e:
        _safe_rm(tmp)
        return SfsResult(False, dst, f"deplacement echoue : {e}")
    finally:
        _safe_rm(tmp)
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    log(f"OK {dst} ({size_mb:.0f} Mo)")
    # VALIDATION post-creation : le SFS est-il reellement montable + bon contenu ?
    try:
        import validate_boot
        kind = "rootfs" if "rootfs" in os.path.basename(dst) else (
            "modules" if "modules" in os.path.basename(dst) else "any")
        chk = validate_boot.validate_sfs(dst, kind=kind, log=lambda m: None)
        if not chk.ok:
            log(f"  [!] VALIDATION ECHOUEE : {chk.reason}")
            return SfsResult(False, dst, f"sfs cree mais invalide : {chk.reason}",
                             int(size_mb))
        log(f"  validation OK : {chk.reason}")
    except Exception as e:
        log(f"  validation non effectuee ({e}) -- non bloquant")
    return SfsResult(True, dst, "", int(size_mb))


def _same_fs(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _safe_rm(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _sfs_mountpoint(dataset, log):
    """Point de montage REELLEMENT monte du dataset (le monte si besoin).
    CORRECTION du bug : on ne se contente pas de isdir() (un dossier vide non
    monte le passe !) -> on exige ismount() pour ne JAMAIS ecrire dans un dossier
    vide en croyant ecrire sur le dataset."""
    subprocess.run(["zfs", "mount", dataset], stderr=subprocess.DEVNULL)
    try:
        mp = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint",
                             dataset], capture_output=True, text=True).stdout.strip()
    except OSError:
        mp = ""
    if not mp or mp == "legacy" or not os.path.isdir(mp):
        log(f"{dataset} : mountpoint inutilisable (mp={mp!r})")
        return None
    if not os.path.ismount(mp):
        log(f"{dataset} : {mp} existe mais N'EST PAS un point de montage "
            f"(dataset NON monte -> refus d'ecrire dans un dossier vide)")
        return None
    return mp


# --------------------------------------------------------------------------- #
# API publique
# --------------------------------------------------------------------------- #
def build_rootfs_sfs(rootfs_src, sfs_dataset="fast_pool/sfs", name="rootfs.sfs",
                     log=print, force=False):
    """Cree rootfs.sfs depuis l'arborescence rootfs_src (la racine Gentoo a
    figer). Staging tmpfs, nettoyage, controle. Si le fichier existe deja et
    force=False, ne refait rien. Retourne SfsResult."""
    mp = _sfs_mountpoint(sfs_dataset, log)
    if mp is None:
        return SfsResult(False, "", f"{sfs_dataset} non monte")
    dst = os.path.join(mp, name)
    if os.path.exists(dst) and not force:
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        log(f"{dst} existe deja ({size_mb:.0f} Mo) -- pas recree (force=False)")
        return SfsResult(True, dst, "deja present", int(size_mb))
    est = _dir_size_mb(rootfs_src) * 0.5       # zstd ~50 % sur un rootfs
    staging = _pick_staging(est, log)
    return _mksquashfs(rootfs_src, dst, staging, log)


def build_modules_sfs(kver, sfs_dataset="fast_pool/sfs", log=print, force=False):
    """Cree modules-<kver>.sfs depuis /lib/modules/<kver>. Retourne SfsResult."""
    mp = _sfs_mountpoint(sfs_dataset, log)
    if mp is None:
        return SfsResult(False, "", f"{sfs_dataset} non monte")
    src = f"/lib/modules/{kver}"
    dst = os.path.join(mp, f"modules-{kver}.sfs")
    if os.path.exists(dst) and not force:
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        log(f"{dst} existe deja ({size_mb:.0f} Mo) -- pas recree")
        return SfsResult(True, dst, "deja present", int(size_mb))
    est = _dir_size_mb(src) * 0.5
    staging = _pick_staging(est, log)
    return _mksquashfs(src, dst, staging, log)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="creation des images squashfs")
    ap.add_argument("--rootfs-src", help="racine Gentoo a figer en rootfs.sfs")
    ap.add_argument("--modules", help="version noyau pour modules-<ver>.sfs")
    ap.add_argument("--dataset", default="fast_pool/sfs")
    ap.add_argument("--force", action="store_true", help="recree meme si present")
    a = ap.parse_args()
    if not a.rootfs_src and not a.modules:
        ap.error("fournir --rootfs-src et/ou --modules")
    rc = 0
    if a.rootfs_src:
        r = build_rootfs_sfs(a.rootfs_src, a.dataset, force=a.force)
        print(repr(r))
        rc |= 0 if r.ok else 1
    if a.modules:
        r = build_modules_sfs(a.modules, a.dataset, force=a.force)
        print(repr(r))
        rc |= 0 if r.ok else 1
    raise SystemExit(rc)

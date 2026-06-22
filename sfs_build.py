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

# Repertoires a EXCLURE de rootfs.sfs : pseudo-FS (proc/sys/dev/run) et volatils.
# Defense en profondeur : meme si --rootfs-src pointe une racine vivante non
# nettoyee par clean_rootfs, mksquashfs ne fige pas leur contenu. mksquashfs -e
# attend des chemins RELATIFS a la racine source. On garde les repertoires
# (vides) car -e exclut le CONTENU, le dossier reste cree par l'arbo source.
ROOTFS_EXCLUDES = ["proc", "sys", "dev", "run", "tmp", "var/tmp",
                   "mnt", "media", "lost+found",
                   "var/cache/distfiles", "var/cache/binpkgs",
                   "var/tmp/portage", ".cleaned-for-sfs"]

CLEANED_MARKER = ".cleaned-for-sfs"


def _is_clean_copy(rootfs_src):
    """Le repertoire porte-t-il le marqueur depose par clean_rootfs ? (=> c'est
    une copie nettoyee, pas le systeme vivant)."""
    return os.path.exists(os.path.join(rootfs_src, CLEANED_MARKER))


def _looks_live(rootfs_src):
    """Heuristique : rootfs_src est-il (ou contient-il) le systeme VIVANT ?
    - c'est '/' lui-meme, ou
    - un de ses sous-dossiers pseudo-FS est monte (proc/sys/dev actifs) -> signe
      d'une racine en service, pas d'une copie inerte."""
    src = os.path.abspath(rootfs_src)
    if src == "/":
        return True
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and p[2] in ("proc", "sysfs", "devtmpfs"):
                    mnt = os.path.abspath(p[1])
                    # un pseudo-FS monte SOUS la source = racine vivante
                    if mnt == os.path.join(src, p[2]) or mnt.startswith(src + "/proc") \
                       or mnt.startswith(src + "/sys") or mnt.startswith(src + "/dev"):
                        return True
    except OSError:
        pass
    return False


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


def _mksquashfs(src, dst, staging, log, extra=None):
    """Compresse src -> dst via mksquashfs, en ecrivant le fichier temporaire
    DIRECTEMENT dans le dossier de destination (meme FS que dst). Ainsi la
    publication est TOUJOURS un os.replace atomique et instantane -- JAMAIS de
    copie du .sfs (qui peut faire des Go). Plus de staging intermediaire :
    mksquashfs ecrit a cote de la cible finale, c'est tout.

    `staging` est ignore pour l'ecriture du .sfs (conserve dans la signature pour
    compat) ; mksquashfs gere lui-meme sa memoire de travail."""
    if not os.path.isdir(src):
        return SfsResult(False, dst, f"source absente : {src}")
    dst_dir = os.path.dirname(dst) or "."
    os.makedirs(dst_dir, exist_ok=True)
    # .new dans le MEME dossier que dst -> meme FS -> os.replace atomique garanti.
    tmp = dst + ".new"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    cmd = ["mksquashfs", src, tmp, "-comp", "zstd", "-Xcompression-level", "19",
           "-xattrs", "-noappend", "-quiet", "-processors", str(os.cpu_count() or 1)]
    if extra:
        cmd += extra
    log(f"mksquashfs {src} -> {dst} (ecriture directe, sans re-staging)")
    try:
        p = subprocess.run(cmd)
    except FileNotFoundError:
        return SfsResult(False, dst, "mksquashfs introuvable "
                         "(installe sys-fs/squashfs-tools)")
    if p.returncode != 0:
        _safe_rm(tmp)
        return SfsResult(False, dst, f"mksquashfs a echoue (rc={p.returncode})")
    try:
        size = os.path.getsize(tmp)
    except OSError:
        return SfsResult(False, dst, "fichier squashfs introuvable apres creation")
    if size < 4096:
        _safe_rm(tmp)
        return SfsResult(False, dst, f"squashfs anormalement petit ({size} o)")
    # publication : os.replace LOCAL (tmp et dst dans le meme dossier) -> atomique,
    # instantane, jamais cross-device.
    moved = False
    try:
        os.replace(tmp, dst)
        moved = True
    except OSError as e:
        _safe_rm(tmp)
        _safe_rm(dst + ".new")
        return SfsResult(False, dst, f"publication echouee : {e}")
    if not moved:
        return SfsResult(False, dst, "publication non effectuee")
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


def _safe_rm(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def _sfs_mountpoint(dataset, log):
    """Point de montage REELLEMENT monte du dataset (le monte si besoin).
    On NE se fie PAS a os.path.ismount() : peu fiable sur ZFS en chroot (st_dev
    trompeur). Verite terrain = `zfs get mounted` + /proc/mounts."""
    subprocess.run(["zfs", "mount", dataset], stderr=subprocess.DEVNULL)
    try:
        mp = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint",
                             dataset], capture_output=True, text=True).stdout.strip()
    except OSError:
        mp = ""
    if not mp or mp == "legacy" or not os.path.isdir(mp):
        log(f"{dataset} : mountpoint inutilisable (mp={mp!r})")
        return None
    # monte ? source de verite : zfs get mounted, puis /proc/mounts en secours.
    mounted = False
    try:
        m = subprocess.run(["zfs", "get", "-H", "-o", "value", "mounted",
                            dataset], capture_output=True, text=True).stdout.strip()
        mounted = (m == "yes")
    except OSError:
        pass
    if not mounted:
        mounted = _in_proc_mounts(dataset)
    if not mounted:
        log(f"{dataset} : reellement NON monte (zfs mounted=no, absent de "
            f"/proc/mounts) -> refus d'ecrire dans un dossier vide")
        return None
    return mp


def _in_proc_mounts(dataset):
    """Le dataset apparait-il dans /proc/mounts (verite terrain) ?"""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and p[2] == "zfs" and p[0] == dataset:
                    return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------- #
# API publique
# --------------------------------------------------------------------------- #
APPLIANCE_SCRIPTS = {
    # source (repertoire de reference) -> destination DANS le rootfs
    "session_launch.py": "sbin/session_launch.py",
    "boot_confirm.py":   "usr/local/sbin/boot_confirm.py",
    "infra.conf":        "etc/infra.conf",   # config lue par start_services au boot
}


def deploy_appliance_scripts(rootfs_src, ref_dir, log=print):
    """Copie les scripts 'appliance' (session_launch.py, boot_confirm.py) depuis
    ref_dir vers le rootfs AVANT de figer le sfs. SANS ca, le sfs figerait la
    version DEJA presente dans le rootfs -> on debugge avec du vieux code (cause
    du panic cage 'execvp' alors que le fix etait livre ailleurs).
    ref_dir = repertoire ou se trouvent les .py a jour (defaut : a cote de
    sfs_build.py, ou via APPLIANCE_REF env)."""
    if not ref_dir:
        ref_dir = os.environ.get("APPLIANCE_REF") or os.path.dirname(
            os.path.abspath(__file__))
    deployed, missing = [], []
    for srcname, relpath in APPLIANCE_SCRIPTS.items():
        src = os.path.join(ref_dir, srcname)
        if not os.path.isfile(src):
            missing.append(srcname)
            continue
        dst = os.path.join(rootfs_src, relpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        # executable pour les scripts, lecture seule pour la config
        os.chmod(dst, 0o644 if srcname.endswith((".conf", ".ini")) else 0o755)
        deployed.append(relpath)
        log(f"  deploye {srcname} -> rootfs:/{relpath}")
    if deployed:
        log(f"scripts appliance a jour dans le rootfs ({len(deployed)}) avant sfs")
    if missing:
        log(f"  [!] scripts introuvables dans {ref_dir} : {', '.join(missing)} "
            f"-> le sfs gardera la version deja dans le rootfs (peut etre vieille)")
    return deployed, missing


def build_rootfs_sfs(rootfs_src, sfs_dataset="fast_pool/sfs", name="rootfs.sfs",
                     log=print, force=False, force_live=False, ref_dir=None,
                     versioned=True):
    """Cree le rootfs squashfs depuis l'arborescence rootfs_src. rootfs_src DOIT
    etre une copie nettoyee par clean_rootfs (marqueur .cleaned-for-sfs) ; sinon
    on REFUSE de figer (risque : figer le systeme vivant). force_live passe outre.

    versioned=True (defaut, CONFORME a l'architecture) : ecrit 'rootfs-vN.sfs'
    (N = version suivante) et (re)pointe le lien 'rootfs.sfs' -> 'rootfs-vN.sfs'.
    C'est ce que select_rootfs (use/rollback) et freeze_overlay attendent en aval
    ; un fichier plat 'rootfs.sfs' casserait _active()/_versions(). versioned=False
    revient a l'ancien fichier plat (deconseille). Retourne SfsResult."""
    if not os.path.isdir(rootfs_src):
        return SfsResult(False, "", f"source absente : {rootfs_src}")
    # GARDE : ne pas figer le systeme vivant par erreur
    if not _is_clean_copy(rootfs_src):
        if _looks_live(rootfs_src) and not force_live:
            return SfsResult(False, "", (
                f"{rootfs_src} semble etre le systeme VIVANT (pseudo-FS montes "
                f"dessous) et n'a PAS le marqueur clean_rootfs. Refus de figer. "
                f"Fais d'abord : clean_rootfs.py --source ... --staging {rootfs_src} "
                f"(ou --force-live pour passer outre)."))
        if not force_live:
            log(f"  [!] {rootfs_src} sans marqueur .cleaned-for-sfs : ce n'est "
                f"peut-etre pas une copie nettoyee par clean_rootfs.")
            log(f"      -> recommande : clean_rootfs.py d'abord. "
                f"(--force-live pour ignorer cet avertissement)")
            return SfsResult(False, "", (
                "source non marquee comme nettoyee (clean_rootfs). "
                "Utilise clean_rootfs ou --force-live."))
    mp = _sfs_mountpoint(sfs_dataset, log)
    if mp is None:
        return SfsResult(False, "", f"{sfs_dataset} non monte")
    # DEPLOIEMENT : copier les scripts appliance A JOUR dans le rootfs AVANT de
    # figer. Sinon le sfs garderait la version deja presente -> vieux code fige.
    deploy_appliance_scripts(rootfs_src, ref_dir, log=log)
    # exclusions pseudo-FS/volatils : -e doit etre le DERNIER flag mksquashfs
    # (tout ce qui suit est traite comme motif d'exclusion).
    extra = ["-e"] + ROOTFS_EXCLUDES

    if versioned:
        # MODELE CANONIQUE : rootfs-vN.sfs + lien rootfs.sfs -> rootfs-vN.sfs.
        # On REUTILISE select_rootfs (versionnage + lien atomique) -- pas de
        # logique parallele. Chaque build = une nouvelle version (comme
        # freeze_overlay) ; l'ancienne est memorisee pour rollback.
        try:
            import select_rootfs
        except Exception as e:
            return SfsResult(False, "", f"select_rootfs indisponible : {e}")
        vers = select_rootfs._versions(mp)
        nextn = (vers[-1][0] + 1) if vers else 1
        target = f"rootfs-v{nextn}.sfs"
        dst = os.path.join(mp, target)
        res = _mksquashfs(rootfs_src, dst, mp, log, extra=extra)
        if not res.ok:
            return res
        select_rootfs._set_link(mp, target)        # publication atomique du lien
        log(f"  rootfs versionne : {name} -> {target} (v{nextn})")
        return res

    # MODE NON VERSIONNE (deconseille, NON conforme) : fichier plat 'name'.
    dst = os.path.join(mp, name)
    if os.path.exists(dst) and not force:
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        log(f"{dst} existe deja ({size_mb:.0f} Mo) -- pas recree (force=False)")
        return SfsResult(True, dst, "deja present", int(size_mb))
    return _mksquashfs(rootfs_src, dst, mp, log, extra=extra)


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
    # ecriture directe (pas de staging) : .new a cote de dst + os.replace atomique
    return _mksquashfs(src, dst, mp, log)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="creation des images squashfs")
    ap.add_argument("--rootfs-src", help="racine Gentoo a figer en rootfs.sfs")
    ap.add_argument("--modules", help="version noyau pour modules-<ver>.sfs")
    ap.add_argument("--dataset", default="fast_pool/sfs")
    ap.add_argument("--force", action="store_true", help="recree meme si present")
    ap.add_argument("--force-live", action="store_true",
                    help="autorise a figer une racine sans marqueur clean_rootfs "
                         "(systeme vivant) -- a tes risques")
    ap.add_argument("--no-versioned", action="store_true",
                    help="ecrit un fichier plat rootfs.sfs au lieu du modele "
                         "versionne rootfs-vN.sfs + lien (DECONSEILLE, non conforme)")
    a = ap.parse_args()
    if not a.rootfs_src and not a.modules:
        ap.error("fournir --rootfs-src et/ou --modules")
    rc = 0
    if a.rootfs_src:
        r = build_rootfs_sfs(a.rootfs_src, a.dataset, force=a.force,
                             force_live=a.force_live,
                             versioned=not a.no_versioned)
        print(repr(r))
        rc |= 0 if r.ok else 1
    if a.modules:
        r = build_modules_sfs(a.modules, a.dataset, force=a.force)
        print(repr(r))
        rc |= 0 if r.ok else 1
    raise SystemExit(rc)

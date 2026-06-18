#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
clean_rootfs.py — preparer une racine Gentoo PROPRE pour figer en rootfs.sfs.

Principe (sur) : on ne nettoie JAMAIS le systeme source. On copie le rootfs
source vers un staging fourni (rsync -aHAX : preserve xattr/ACL/hardlinks/perm),
puis on nettoie LA COPIE. Le staging propre est ensuite passe a
  python3 sfs_build.py --rootfs-src <staging>

Jamais automatique : ce module ne s'execute que si on le lance explicitement, et
il demande confirmation avant toute suppression (sauf --yes).

Garde-fous stricts :
  - refuse si staging == '/' ou un chemin systeme (/usr, /etc, /var, /bin...)
  - refuse si source et staging se recouvrent
  - n'agit que dans le staging, jamais ailleurs

ASCII-only, stdlib + rsync systeme.
"""
import argparse
import os
import shutil
import subprocess
import sys

# chemins systeme interdits comme staging (on ne nettoie jamais ca)
FORBIDDEN = ("/", "/usr", "/etc", "/var", "/bin", "/sbin", "/lib", "/lib64",
             "/boot", "/root", "/home", "/opt", "/run", "/proc", "/sys", "/dev")

# elements a SUPPRIMER de la copie (chemins relatifs a la racine du staging).
# Caches/sources/logs : lourds et inutiles dans une image lecture seule.
PURGE_DIRS = [
    "var/tmp/portage",          # residus de compilation (souvent plusieurs Go)
    "var/cache/distfiles",      # tarballs sources telecharges
    "var/cache/binpkgs",        # paquets binaires
    "usr/portage",              # arbre Portage legacy
    "run",                      # etat de session volatil
    "tmp",                      # temporaires
]
PURGE_GLOBS = [
    "var/log/*.log", "var/log/*/*.log",      # logs de build
    "root/.bash_history", "root/.viminfo",   # historiques de la machine de build
    "etc/ssh/ssh_host_*",                    # clefs SSH (regenerees au 1er boot)
]
# fichiers a vider/supprimer specifiquement
PURGE_FILES = [
    "etc/machine-id",                        # doit etre regenere, pas fige
    "etc/yt.key", "etc/initramfs-stream.pid",  # handoff initramfs (cf. init.py)
    "etc/resolv.conf",                       # specifique a la machine de build
    "var/lib/portage/world.lock",
]
# ce qu'on NE touche JAMAIS (avertissement si quelqu'un veut l'ajouter)
PROTECTED = ("etc/portage", "var/db/pkg", "lib/modules",
             "sbin/session_launch.py", "usr/local/sbin/boot_confirm.py")


def log(m):
    print(f">> {m}", flush=True)


def _abort(m):
    sys.exit(f"ABANDON : {m}")


def _norm(p):
    return os.path.normpath(os.path.abspath(p))


def safety_checks(source, staging):
    """Garde-fous AVANT toute action. Leve SystemExit si dangereux."""
    source = _norm(source)
    staging = _norm(staging)
    if staging in (_norm(f) for f in FORBIDDEN):
        _abort(f"staging '{staging}' est un chemin systeme interdit.")
    if staging == "/" or staging.count("/") < 2:
        _abort(f"staging '{staging}' trop proche de la racine (dangereux).")
    if source == staging:
        _abort("source et staging identiques.")
    # recouvrement : staging dans source ou l'inverse
    if staging.startswith(source + os.sep) or source.startswith(staging + os.sep):
        _abort(f"source ({source}) et staging ({staging}) se recouvrent.")
    if not os.path.isdir(source):
        _abort(f"source introuvable : {source}")
    return source, staging


# Pseudo-systemes de fichiers et repertoires volatils a NE JAMAIS copier dans
# l'image : si la racine source les a montes, rsync descendrait dans des fichiers
# virtuels (ex /proc/kcore = taille de la RAM -> blocage/explosion) ou copierait
# l'etat volatil de la machine de build. On veut des POINTS DE MONTAGE VIDES dans
# l'image (l'initramfs/le systeme les remplit au boot), pas leur contenu.
RSYNC_EXCLUDES = [
    "/proc/*", "/sys/*", "/dev/*", "/run/*",       # pseudo-FS (contenu, pas le dir)
    "/tmp/*", "/var/tmp/*",                          # volatils
    "/mnt/*", "/media/*",                            # points de montage externes
    "/lost+found",
    "/var/cache/distfiles/*", "/var/cache/binpkgs/*",  # gros caches Portage
    "/var/tmp/portage/*",
]


def rsync_copy(source, staging, dry=False):
    """Copie source -> staging avec preservation totale (xattr/ACL/hardlinks),
    en EXCLUANT les pseudo-FS (proc/sys/dev/run) et les volatils. Les dossiers
    eux-memes sont conserves (vides) car on exclut '/proc/*' et non '/proc'.
    Le slash final sur source copie le CONTENU dans staging."""
    os.makedirs(staging, exist_ok=True)
    cmd = ["rsync", "-aHAX", "--numeric-ids", "--info=progress2",
           "--one-file-system"]              # ne traverse pas les points de montage
    for ex in RSYNC_EXCLUDES:
        cmd += ["--exclude", ex]
    cmd += [source.rstrip("/") + "/", staging.rstrip("/") + "/"]
    if dry:
        cmd.insert(1, "--dry-run")
    log("rsync " + ("(dry-run) " if dry else "")
        + f"{source} -> {staging} (pseudo-FS exclus)")
    try:
        rc = subprocess.run(cmd).returncode
    except FileNotFoundError:
        _abort("rsync introuvable (emerge net-misc/rsync).")
    if rc != 0:
        _abort(f"rsync a echoue (rc={rc}).")


def _rm_path(p, dry):
    if not os.path.lexists(p):
        return 0
    if dry:
        log(f"  [dry] supprimerait {p}")
        return 1
    try:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.unlink(p)
        return 1
    except OSError as e:
        log(f"  [!] {p} : {e}")
        return 0


def clean_copy(staging, dry=False):
    """Nettoie LA COPIE (staging) uniquement. Retourne le nb d'elements traites.
    Tous les chemins sont confines sous staging."""
    import glob
    staging = _norm(staging)
    n = 0
    # repertoires de cache/temp
    for rel in PURGE_DIRS:
        p = os.path.join(staging, rel)
        if rel in PROTECTED:               # double securite
            continue
        n += _rm_path(p, dry)
        if not dry:
            os.makedirs(p, exist_ok=True)  # recree le dossier vide (ex: /tmp, /run)
    # globs
    for pat in PURGE_GLOBS:
        for p in glob.glob(os.path.join(staging, pat)):
            n += _rm_path(p, dry)
    # fichiers specifiques
    for rel in PURGE_FILES:
        n += _rm_path(os.path.join(staging, rel), dry)
    # caches python (mauvaise version figee = piege)
    if not dry:
        for root, dirs, files in os.walk(staging):
            for d in list(dirs):
                if d == "__pycache__":
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    dirs.remove(d)
                    n += 1
            for f in files:
                if f.endswith(".pyc"):
                    _rm_path(os.path.join(root, f), False)
    else:
        log("  [dry] supprimerait les __pycache__ et *.pyc")
    # machine-id : fichier vide present (regenere au boot)
    if not dry:
        mid = os.path.join(staging, "etc/machine-id")
        try:
            os.makedirs(os.path.dirname(mid), exist_ok=True)
            open(mid, "w").close()
        except OSError:
            pass
    # POINTS DE MONTAGE VIDES : doivent exister dans l'image (l'initramfs/le
    # systeme y montent les pseudo-FS au boot), mais VIDES (jamais le contenu
    # de la machine de build). Exclus du rsync, on s'assure qu'ils existent.
    if not dry:
        for d in ("proc", "sys", "dev", "run", "tmp", "mnt", "media",
                  "mnt/usr-src", "var/log"):
            try:
                os.makedirs(os.path.join(staging, d), exist_ok=True)
            except OSError:
                pass
        # /tmp en 1777 (sticky), classique
        try:
            os.chmod(os.path.join(staging, "tmp"), 0o1777)
        except OSError:
            pass
    else:
        log("  [dry] garantirait les points de montage vides "
            "(proc/sys/dev/run/tmp/mnt + mnt/usr-src + var/log)")
    return n


def verify_essentials(staging):
    """Verifie que la copie nettoyee contient toujours l'essentiel (sinon le
    boot echouera). Avertit, ne bloque pas."""
    staging = _norm(staging)
    must = ["sbin/session_launch.py", "usr/local/sbin/boot_confirm.py",
            "lib/modules", "etc/portage", "var/db/pkg"]
    missing = [m for m in must if not os.path.lexists(os.path.join(staging, m))]
    if missing:
        log("ATTENTION : elements essentiels absents de la copie : "
            + ", ".join(missing))
    else:
        log("essentiels presents (session_launch, boot_confirm, modules, portage).")


def main():
    ap = argparse.ArgumentParser(
        description="prepare une racine Gentoo propre (sur une COPIE) pour rootfs.sfs")
    ap.add_argument("--source", required=True, help="racine Gentoo source (intacte)")
    ap.add_argument("--staging", required=True,
                    help="ou copier+nettoyer (doit avoir la place ; PAS un chemin systeme)")
    ap.add_argument("--yes", action="store_true", help="ne pas demander confirmation")
    ap.add_argument("--dry-run", action="store_true",
                    help="montre ce qui serait copie/supprime sans rien faire")
    ap.add_argument("--no-copy", action="store_true",
                    help="le staging est deja une copie : nettoyer sans recopier")
    a = ap.parse_args()

    source, staging = safety_checks(a.source, a.staging)
    log(f"source (intacte) : {source}")
    log(f"staging (copie a nettoyer) : {staging}")

    if not a.yes and not a.dry_run:
        try:
            ans = input(f"Copier {source} -> {staging} puis NETTOYER la copie ? "
                        f"[y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("y", "o", "yes", "oui"):
            _abort("annule par l'utilisateur.")

    if not a.no_copy:
        rsync_copy(source, staging, dry=a.dry_run)
    else:
        log("--no-copy : nettoyage direct du staging (suppose deja une copie)")

    n = clean_copy(staging, dry=a.dry_run)
    log(f"{'(dry) ' if a.dry_run else ''}{n} element(s) nettoye(s).")
    verify_essentials(staging)

    if not a.dry_run:
        log("copie propre prete. Etape suivante :")
        log(f"  python3 sfs_build.py --rootfs-src {staging}")


if __name__ == "__main__":
    main()

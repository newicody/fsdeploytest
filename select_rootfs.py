#!/usr/bin/env python3
"""select_rootfs.py -- choisit quelle version de rootfs booter.

Mecanisme : 'rootfs.sfs' est un LIEN SYMBOLIQUE vers 'rootfs-vN.sfs' dans le
dataset sfs (fast_pool/sfs). init.py monte toujours 'rootfs.sfs' (le lien suivi
de maniere transparente par os.open/losetup), donc changer de version = recreer
le lien. Rollback trivial : re-pointer vers une version anterieure.

Commandes :
  select_rootfs.py list              # liste les versions + l'active
  select_rootfs.py use <vN|fichier>  # active cette version (recree le lien)
  select_rootfs.py rollback          # revient a la version precedente (memo)

Le changement prend effet au PROCHAIN boot. L'upper se reinitialise alors (le
CRC du sfs change -> mecanisme upper_stale d'init.py), donc tes modifs non figees
de l'overlay courant seront perdues : fige-les avec freeze_overlay.py AVANT de
changer de version si tu veux les garder.

ASCII-only, stdlib + zfs. configobj non requis ici.
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


POOL = os.environ.get("ROOTFS_POOL", "fast_pool")
SFS_DS = os.environ.get("ROOTFS_SFS_DS", f"{POOL}/sfs")
LINK_NAME = "rootfs.sfs"
PREV_MEMO = ".rootfs-previous"     # memorise la version precedente (rollback)


def _sfs_dir():
    rc, mp = _capture(["zfs", "get", "-H", "-o", "value", "mountpoint", SFS_DS])
    if rc == 0 and mp and mp not in ("legacy", "none") and os.path.isdir(mp):
        return mp
    for c in (f"/{SFS_DS}", "/mnt/sfs"):
        if os.path.isdir(c):
            return c
    return ""


def _versions(sfs_dir):
    """Liste triee des fichiers rootfs-vN.sfs presents."""
    out = []
    try:
        for f in os.listdir(sfs_dir):
            if f.startswith("rootfs-v") and f.endswith(".sfs"):
                try:
                    n = int(f[len("rootfs-v"):-len(".sfs")])
                    out.append((n, f))
                except ValueError:
                    pass
    except OSError:
        pass
    out.sort()
    return out


def _active(sfs_dir):
    """Nom du fichier vers lequel pointe le lien rootfs.sfs (ou None)."""
    link = os.path.join(sfs_dir, LINK_NAME)
    if os.path.islink(link):
        return os.path.basename(os.readlink(link))
    if os.path.isfile(link):
        return LINK_NAME + " (fichier reel, pas un lien)"
    return None


def cmd_list():
    sfs_dir = _sfs_dir()
    if not sfs_dir:
        sys.exit(f"dataset sfs ({SFS_DS}) introuvable/non monte.")
    active = _active(sfs_dir)
    vers = _versions(sfs_dir)
    log(f"dataset sfs : {sfs_dir}")
    log(f"actif (rootfs.sfs -> ) : {active or 'AUCUN'}")
    if not vers:
        log("aucune version rootfs-vN.sfs (cree-en une avec freeze_overlay.py)")
        return
    log("versions disponibles :")
    for n, f in vers:
        size = ""
        try:
            mb = os.path.getsize(os.path.join(sfs_dir, f)) // (1024 * 1024)
            size = f" ({mb} Mo)"
        except OSError:
            pass
        mark = "  <== actif" if f == active else ""
        log(f"  v{n:<3} {f}{size}{mark}")


def _set_link(sfs_dir, target_file):
    """(Re)cree le lien rootfs.sfs -> target_file, en memorisant l'ancien."""
    link = os.path.join(sfs_dir, LINK_NAME)
    target_path = os.path.join(sfs_dir, target_file)
    if not os.path.isfile(target_path):
        sys.exit(f"cible introuvable : {target_path}")
    # memoriser l'actuel pour le rollback
    cur = _active(sfs_dir)
    if cur and cur.endswith(".sfs"):
        try:
            with open(os.path.join(sfs_dir, PREV_MEMO), "w") as f:
                f.write(cur + "\n")
        except OSError:
            pass
    # remplacement ATOMIQUE du lien : creer un lien temporaire puis rename
    tmp = link + ".new"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(target_file, tmp)               # lien RELATIF (meme dossier)
    os.replace(tmp, link)                      # atomique
    log(f"actif : {LINK_NAME} -> {target_file}")
    log("effet au PROCHAIN boot. (l'upper se reinitialisera : le CRC du sfs "
        "change. Fige l'overlay AVANT si tu veux garder tes modifs courantes.)")


def cmd_use(arg):
    sfs_dir = _sfs_dir()
    if not sfs_dir:
        sys.exit(f"dataset sfs ({SFS_DS}) introuvable/non monte.")
    # accepter 'v4', '4', ou 'rootfs-v4.sfs'
    target = arg
    if arg.isdigit():
        target = f"rootfs-v{arg}.sfs"
    elif arg.startswith("v") and arg[1:].isdigit():
        target = f"rootfs-v{arg[1:]}.sfs"
    _set_link(sfs_dir, target)


def cmd_rollback():
    sfs_dir = _sfs_dir()
    if not sfs_dir:
        sys.exit(f"dataset sfs ({SFS_DS}) introuvable/non monte.")
    memo = os.path.join(sfs_dir, PREV_MEMO)
    if not os.path.isfile(memo):
        sys.exit("aucune version precedente memorisee (PREV_MEMO absent).")
    with open(memo) as f:
        prev = f.read().strip()
    if not prev:
        sys.exit("memo de rollback vide.")
    log(f"rollback vers : {prev}")
    _set_link(sfs_dir, prev)


def main():
    if os.geteuid() != 0:
        sys.exit("select_rootfs doit etre lance en root (modifie le lien sfs).")
    if len(sys.argv) < 2:
        log("usage : select_rootfs.py [list | use <vN> | rollback]")
        cmd_list()
        return
    cmd = sys.argv[1]
    if cmd == "list":
        cmd_list()
    elif cmd == "use":
        if len(sys.argv) < 3:
            sys.exit("usage : select_rootfs.py use <vN|fichier>")
        cmd_use(sys.argv[2])
    elif cmd == "rollback":
        cmd_rollback()
    else:
        sys.exit(f"commande inconnue : {cmd} (list|use|rollback)")


if __name__ == "__main__":
    main()

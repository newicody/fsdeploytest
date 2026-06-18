#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
zfs_mounts.py — detection / verification / montage de datasets ZFS (reutilisable).

Lecon du bug : creer un point de montage (os.makedirs) ne garantit PAS que le
dataset est monte. On se retrouve avec un dossier VIDE la ou on croyait avoir un
dataset -> donnees ecrites au mauvais endroit, rootfs pollue. Ce module ne
suppose JAMAIS qu'un montage a reussi : il VERIFIE (ismount + /proc/mounts +,
si demande, presence d'un contenu attendu) avant de considerer un dataset comme
disponible.

Utilisable dans first_boot.py, kernel_build.py, sfs_build.py. (init.py reste
autonome dans l'initramfs avec sa propre logique mount_dataset, meme esprit.)

Retour observable (MountState), pas d'exception qui fuit. ASCII-only, stdlib.
"""
import os
import subprocess


class MountState:
    """Etat verifie d'un dataset. On LIT les attributs au lieu de supposer."""
    __slots__ = ("dataset", "exists", "mounted", "mountpoint", "where",
                 "verified", "reason")

    def __init__(self, dataset):
        self.dataset = dataset
        self.exists = False         # le dataset existe dans le pool
        self.mounted = False        # il est REELLEMENT monte (ismount confirme)
        self.mountpoint = ""        # propriete mountpoint ZFS (legacy / /chemin)
        self.where = ""             # ou il est effectivement monte (/proc/mounts)
        self.verified = False       # montage + contenu attendu confirmes
        self.reason = ""

    def __bool__(self):
        return self.mounted

    def __repr__(self):
        return (f"<MountState {self.dataset} exists={self.exists} "
                f"mounted={self.mounted} where={self.where or '-'} "
                f"verified={self.verified}>")


try:
    from common import (sh as _sh, dataset_exists, zfs_get as zfs_property,
                        proc_mounts as _proc_mounts, where_mounted)
except ImportError:
    def _sh(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except OSError as e:
            return 1, "", str(e)

    def dataset_exists(dataset):
        rc, _, _ = _sh(["zfs", "list", "-H", "-o", "name", dataset])
        return rc == 0

    def zfs_property(dataset, prop):
        rc, out, _ = _sh(["zfs", "get", "-H", "-o", "value", prop, dataset])
        return out if rc == 0 else ""

    def _proc_mounts():
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        yield parts[0], parts[1], parts[2]
        except OSError:
            return

    def where_mounted(dataset):
        for source, target, fstype in _proc_mounts():
            if fstype == "zfs" and source == dataset:
                return target
        return ""


def inspect(dataset):
    """Etat VERIFIE d'un dataset (sans rien monter). Remplit MountState."""
    st = MountState(dataset)
    st.exists = dataset_exists(dataset)
    if not st.exists:
        st.reason = "dataset absent du pool"
        return st
    st.mountpoint = zfs_property(dataset, "mountpoint")
    st.where = where_mounted(dataset)
    # VERITE TERRAIN : present dans /proc/mounts (where) OU propriete ZFS
    # mounted=yes. On EVITE os.path.ismount : peu fiable sur ZFS en chroot
    # (st_dev trompeur) -> c'etait la cause du "monte vu comme non monte".
    st.mounted = bool(st.where) or (zfs_property(dataset, "mounted") == "yes")
    if st.mounted and not st.where:
        # monte selon ZFS mais absent de /proc/mounts : retomber sur le mountpoint
        st.where = st.mountpoint if st.mountpoint not in ("", "legacy") else ""
    if not st.mounted:
        st.reason = "dataset existe mais N'EST PAS monte"
    return st


def verify_mounted(dataset, expect_any=None):
    """Verifie qu'un dataset est REELLEMENT monte (pas un dossier vide). Si
    expect_any est fourni (liste de noms de fichiers/dossiers attendus), verifie
    aussi qu'au moins un est present -> distingue 'monte' de 'monte mais vide'.
    Retourne MountState avec .verified positionne."""
    st = inspect(dataset)
    if not st.mounted:
        return st
    if expect_any:
        try:
            present = set(os.listdir(st.where))
        except OSError:
            present = set()
        if not (present & set(expect_any)):
            st.reason = (f"monte sur {st.where} mais contenu attendu absent "
                         f"({expect_any}) -- montage suspect / dossier vide ?")
            return st
    st.verified = True
    st.reason = "monte et verifie"
    return st


def _is_nonempty(path):
    """Le dossier existe-t-il ET contient-il deja des fichiers ? (risque de
    masquage si on monte par-dessus)."""
    try:
        return os.path.isdir(path) and bool(os.listdir(path))
    except OSError:
        return False


def ensure_mounted(dataset, target=None, want_mode="auto", bind_from=None,
                   log=print, expect_any=None, allow_nonempty=False):
    """Monte un dataset SI NECESSAIRE et VERIFIE que c'est reellement monte.
    Ne cree PAS un dossier vide en pretendant que c'est monte.

    GARDE MASQUAGE : si le point de montage (target) contient deja des fichiers,
    monter par-dessus les MASQUE -> on REFUSE (erreur), sauf allow_nonempty=True
    (cas ou le remplacement est voulu, ex: /var/log).

      - non-legacy : zfs mount (ZFS le pose a son mountpoint) ; si target donne
        et different, bind du mountpoint vers target.
      - legacy : mount.zfs dataset target.

    Retourne MountState (verified=True seulement si le montage est confirme).
    """
    st = inspect(dataset)
    if not st.exists:
        log(f"  [!] {dataset} : absent du pool")
        return st

    # GARDE MASQUAGE : refuser de monter sur un dossier non-vide
    if target and not allow_nonempty and _is_nonempty(target):
        # tolerance : si c'est DEJA ce dataset qui est monte la, pas un masquage
        if not (st.mounted and os.path.abspath(st.where) == os.path.abspath(target)):
            log(f"  [!] {dataset} : {target} NON-VIDE -> montage refuse "
                f"(masquerait des fichiers existants ; allow_nonempty pour forcer)")
            st.reason = f"{target} non-vide (masquage refuse)"
            return st

    is_legacy = (st.mountpoint == "legacy")
    if want_mode == "legacy" and not is_legacy:
        log(f"  [!] {dataset} : ini dit legacy mais mountpoint={st.mountpoint}")
    elif want_mode == "property" and is_legacy:
        log(f"  [!] {dataset} : ini dit property mais legacy reel")

    # deja monte au bon endroit ?
    if st.mounted and (target is None or os.path.abspath(st.where) ==
                       os.path.abspath(target)):
        log(f"  {dataset} deja monte sur {st.where}")
        return verify_mounted(dataset, expect_any)

    if is_legacy:
        if target is None:
            log(f"  [!] {dataset} legacy : target requis pour monter")
            st.reason = "legacy sans target"
            return st
        os.makedirs(target, exist_ok=True)
        rc, _, err = _sh(["mount.zfs", dataset, target])
        if rc != 0:
            log(f"  [!] {dataset} (legacy) mount.zfs echoue : {err[:80]}")
            st.reason = "mount.zfs echoue"
            return st
    else:
        # non-legacy : laisser ZFS monter a son mountpoint
        _sh(["zfs", "mount", dataset])
        st2 = inspect(dataset)
        if not st2.mounted:
            log(f"  [!] {dataset} : zfs mount n'a pas monte (mountpoint="
                f"{st.mountpoint})")
            return st2
        # bind si un target different est demande
        if target and os.path.abspath(st2.where) != os.path.abspath(target):
            os.makedirs(target, exist_ok=True)
            rc, _, err = _sh(["mount", "--bind", st2.where, target])
            if rc != 0:
                log(f"  [!] bind {st2.where} -> {target} echoue : {err[:80]}")
                st2.reason = "bind echoue"
                return st2
            log(f"  {dataset} : {st2.where} --bind-> {target}")

    # VERIFICATION FINALE : c'est reellement monte (+ contenu si demande)
    final = verify_mounted(dataset, expect_any)
    if target and not is_legacy:
        # verifier le target via /proc/mounts (un bind y apparait comme une
        # ligne dont le 2e champ est le target) plutot qu'os.path.ismount.
        target_abs = os.path.abspath(target)
        bound = any(os.path.abspath(t) == target_abs for _, t, _ in _proc_mounts())
        if not bound:
            log(f"  [!] {dataset} : {target} pas un point de montage apres bind")
            final.verified = False
            final.reason = "target non monte apres bind"
    if final.verified or final.mounted:
        log(f"  {dataset} monte et verifie ({final.where or target})")
    return final


def report(datasets, log=print):
    """Genere un rapport d'etat (sans monter) pour une liste de datasets.
    Utile en preflight : voir d'un coup ce qui est monte / vide / absent."""
    for ds in datasets:
        st = inspect(ds)
        flag = "OK " if st.mounted else ("-- " if not st.exists else "!! ")
        log(f"  [{flag}] {ds}: "
            + ("absent" if not st.exists else
               (f"monte sur {st.where}" if st.mounted else
                "EXISTE mais NON MONTE (risque dossier vide)")))
        yield st


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="etat des montages ZFS")
    ap.add_argument("datasets", nargs="+", help="datasets a inspecter")
    ap.add_argument("--mount", action="store_true", help="monter si besoin")
    ap.add_argument("--target", default=None)
    a = ap.parse_args()
    bad = 0
    for ds in a.datasets:
        st = ensure_mounted(ds, a.target) if a.mount else inspect(ds)
        print(repr(st), "->", st.reason)
        if not st.mounted:
            bad += 1
    raise SystemExit(1 if bad else 0)

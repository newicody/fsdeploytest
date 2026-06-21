#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
boot_layout.py — source de verite unique pour les ESP et leurs montages.

Resout le DECALAGE entre :
  - point de montage D'INSTALLATION (install_mount, echafaudage chroot/USB), et
  - IDENTITE FINALE (PARTUUID, stable) qui sert a creer l'entree EFI.

Une entree EFI reference une PARTITION (disque+num, derives du PARTUUID) + un
chemin RELATIF a la racine de l'ESP. JAMAIS un point de montage. Ce module
centralise : resolution PARTUUID -> /dev courant, montage a install_mount,
identite (disk, part) pour efibootmgr, garde anti-/boot.

Userspace uniquement (importe common). ASCII-only.
"""
import os
import re

try:
    from common import sh, is_true, load_config
except ImportError:
    import subprocess

    def sh(cmd, timeout=None, check=False):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except OSError as e:
            return 1, "", str(e)

    def is_true(v):
        return str(v).strip().lower() in ("true", "1", "yes", "oui")

    load_config = None


# chemins interdits comme install_mount (on n'empiete pas sur /boot)
FORBIDDEN_INSTALL = ("/boot", "/boot/efi", "/", "/usr", "/etc", "/var")


class Esp:
    """Une ESP resolue : identite finale + echafaudage d'installation."""
    __slots__ = ("name", "partuuid", "partition", "install_mount", "primary",
                 "register_uefi", "tag", "_dev", "reason")

    def __init__(self, name, partuuid="", partition="", install_mount="",
                 primary=False, register_uefi=False, tag=""):
        self.name = name
        self.partuuid = partuuid
        self.partition = partition
        self.install_mount = install_mount
        self.primary = primary
        self.register_uefi = register_uefi
        self.tag = tag or name        # suffixe de label (ex 'nvme0') ; defaut = nom
        self._dev = ""             # device reel resolu (cache)
        self.reason = ""

    def __repr__(self):
        return (f"<Esp {self.name} partuuid={self.partuuid or '-'} "
                f"dev={self._dev or self.partition or '?'} "
                f"install={self.install_mount} primary={self.primary}>")

    # --- resolution de l'identite -----------------------------------------
    def device(self):
        """Device /dev courant. Priorite au PARTUUID (stable) ; repli sur
        'partition' declaree. '' si introuvable."""
        if self._dev:
            return self._dev
        if self.partuuid:
            # /dev/disk/by-partuuid/<uuid> est le plus simple et fiable
            link = f"/dev/disk/by-partuuid/{self.partuuid}"
            if os.path.exists(link):
                self._dev = os.path.realpath(link)
                return self._dev
            # repli : blkid
            rc, out, _ = sh(["blkid", "-t", f"PARTUUID={self.partuuid}",
                            "-o", "device"])
            if rc == 0 and out:
                self._dev = out.splitlines()[0].strip()
                return self._dev
        if self.partition and os.path.exists(self.partition):
            self._dev = self.partition
            return self._dev
        return ""

    def disk_and_part(self):
        """(disque, numero_partition) pour efibootmgr --disk/--part, derives du
        device courant. Ex: /dev/nvme0n1p1 -> ('/dev/nvme0n1', '1'). '' si KO."""
        dev = self.device()
        if not dev:
            return "", ""
        m = re.match(r"^(/dev/(?:nvme\d+n\d+|mmcblk\d+))p(\d+)$", dev)
        if m:
            return m.group(1), m.group(2)
        m = re.match(r"^(/dev/[a-z]+)(\d+)$", dev)     # /dev/sda1
        if m:
            return m.group(1), m.group(2)
        return "", ""

    def current_partuuid(self):
        """PARTUUID reel du device (pour figer l'ini si on l'avait laisse vide)."""
        dev = self.device()
        if not dev:
            return ""
        rc, out, _ = sh(["blkid", "-s", "PARTUUID", "-o", "value", dev])
        return out if rc == 0 else ""

    # --- echafaudage d'installation ---------------------------------------
    def safe_install_mount(self):
        """Verifie que install_mount n'empiete pas sur /boot etc. Retourne le
        chemin si sur, '' sinon (avec self.reason renseigne)."""
        p = os.path.normpath(self.install_mount or "")
        if not p or p in FORBIDDEN_INSTALL:
            self.reason = (f"install_mount '{p}' interdit (ne pas empieter sur "
                           f"/boot ; utilise /mnt/espN)")
            return ""
        if p.startswith("/boot"):
            self.reason = f"install_mount '{p}' sous /boot : refuse"
            return ""
        return p

    def mount_for_install(self, log=print):
        """Monte l'ESP a son install_mount (vfat). Retourne le chemin monte ou
        ''. Ne touche jamais a /boot. Idempotent (si deja monte la, OK)."""
        mp = self.safe_install_mount()
        if not mp:
            log(f"  [!] {self.name} : {self.reason}")
            return ""
        dev = self.device()
        if not dev:
            log(f"  [!] {self.name} : device introuvable "
                f"(partuuid={self.partuuid or '-'}, partition={self.partition or '-'})")
            return ""
        # deja monte la ?
        for src, tgt, _ in _proc_mounts():
            if src == dev and os.path.abspath(tgt) == os.path.abspath(mp):
                log(f"  {self.name} deja monte sur {mp}")
                return mp
        os.makedirs(mp, exist_ok=True)
        rc, _, err = sh(["mount", "-t", "vfat", dev, mp])
        if rc != 0:
            # peut-etre deja monte ailleurs : le signaler
            here = _where_dev(dev)
            if here:
                log(f"  {self.name} : {dev} deja monte sur {here} (utilise ca)")
                return here
            log(f"  [!] {self.name} : mount {dev} -> {mp} echoue : {err[:80]}")
            return ""
        log(f"  {self.name} : {dev} -> {mp} (install)")
        return mp


def _proc_mounts():
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    yield p[0], p[1], p[2]
    except OSError:
        return


def _where_dev(dev):
    for src, tgt, _ in _proc_mounts():
        if src == dev:
            return tgt
    return ""


def load_esps(infra_conf="infra.conf"):
    """Lit [efi] de infra.conf et retourne la liste des Esp. L'ESP 'primary'
    est en premier."""
    try:
        from configobj import ConfigObj
    except ImportError:
        return []
    if not os.path.exists(infra_conf):
        return []
    cfg = ConfigObj(infra_conf)
    efi = cfg.get("efi", {})
    esps = []
    for name, decl in efi.items():
        if not isinstance(decl, dict):
            continue
        esps.append(Esp(
            name=name,
            partuuid=decl.get("partuuid", "").strip(),
            partition=decl.get("partition", "").strip(),
            install_mount=decl.get("install_mount", f"/mnt/{name}"),
            primary=is_true(decl.get("primary", "false")),
            register_uefi=is_true(decl.get("register_uefi", "false")),
            tag=decl.get("tag", "").strip(),
        ))
    esps.sort(key=lambda e: not e.primary)     # primary d'abord
    return esps


def primary_esp(infra_conf="infra.conf"):
    for e in load_esps(infra_conf):
        if e.primary:
            return e
    esps = load_esps(infra_conf)
    return esps[0] if esps else None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="resolution des ESP (install vs final)")
    ap.add_argument("--infra", default="infra.conf")
    ap.add_argument("--mount", action="store_true", help="monter a install_mount")
    ap.add_argument("--show-partuuid", action="store_true",
                    help="afficher le PARTUUID reel (pour figer l'ini)")
    a = ap.parse_args()
    esps = load_esps(a.infra)
    if not esps:
        raise SystemExit("aucune ESP dans [efi] (ou configobj absent)")
    for e in esps:
        print(repr(e))
        disk, part = e.disk_and_part()
        print(f"   entree EFI -> disque={disk or '?'} part={part or '?'} "
              f"(register_uefi={e.register_uefi})")
        if a.show_partuuid:
            print(f"   PARTUUID reel : {e.current_partuuid() or '(introuvable)'}")
        if a.mount:
            e.mount_for_install()

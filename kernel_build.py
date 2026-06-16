#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
kernel_build.py — Brique 2 de l'auto-update (config .config posee par
kernel_watch.py). Compile, reconstruit zfs-kmod, regenere modules-<ver>.sfs
+ initramfs-<ver>.zst, stage l'ESP, cree une entree EFI versionnee et arme
BootNext (essai unique). On garde les anciens noyaux = fallback. Root requis.
"""
import os
import re
import shutil
import subprocess
import sys

SRC = os.environ.get("SRC", "/usr/src/linux")
ESP = os.environ.get("ESP", "/boot/efi")
DISK = os.environ.get("DISK", "/dev/nvme0n1")
PART = os.environ.get("PART", "1")
BUILD_INITRAMFS = os.environ.get("BUILD_INITRAMFS", "./build_initramfs.py")
JOBS = os.environ.get("JOBS", str(os.cpu_count() or 4))
CMDLINE = os.environ.get(
    "CMDLINE",
    "i915.force_probe=!4c8b xe.force_probe=4c8b "
    "ip=192.168.1.10::192.168.1.1:255.255.255.0::eth0:off:8.8.8.8 "
    "console=tty0 loglevel=4")
DEST_DIR = "EFI/gentoo"


def msg(s):
    print(f">> {s}", flush=True)


def run(cmd, **kw):
    msg("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def out(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def efi_entry(label):
    # NB: efibootmgr affiche 'Boot0002* Gentoo-x.y\tHD(...)/File(...)' : le label
    # est suivi du chemin du loader. On NE doit donc PAS ancrer sur $ (sinon
    # l'entree existe mais n'est pas retrouvee). On matche le label en debut de
    # champ, suivi d'un separateur (tab/espaces/fin).
    m = re.findall(rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{re.escape(label)}(?:\s|$)",
                   out(["efibootmgr"]), re.M)
    return m[0] if m else None


def ensure_efivarfs():
    """efibootmgr a besoin de /sys/firmware/efi/efivars monte (sinon exit 2
    'EFI variables are not supported'). On le monte si besoin (en chroot il ne
    l'est pas). Retourne True si disponible."""
    p = "/sys/firmware/efi/efivars"
    if os.path.ismount(p):
        return True
    try:
        if os.listdir(p):                # non vide -> deja accessible
            return True
    except OSError:
        pass
    subprocess.run(["mount", "-t", "efivarfs", "efivarfs", p],
                   stderr=subprocess.DEVNULL)
    return os.path.ismount(p) or bool(_safe_listdir(p))


def _safe_listdir(p):
    try:
        return os.listdir(p)
    except OSError:
        return []


def main():
    if os.geteuid() != 0:
        sys.exit("root requis")
    if not os.path.ismount(ESP):
        sys.exit(f"ESP non monte sur {ESP}")
    if not os.path.isfile(os.path.join(SRC, ".config")):
        sys.exit(f".config absent dans {SRC} (lance kernel_watch.py d'abord)")

    kver = out(["make", "-C", SRC, "-s", "kernelrelease"]).strip()
    msg(f"build noyau {kver} (-j{JOBS})")

    # 1. noyau + modules in-tree
    run(["make", "-C", SRC, f"-j{JOBS}"])
    run(["make", "-C", SRC, "modules_install"])

    # 2. zfs/spl hors-arbre contre ce noyau (voie Gentoo)
    link = "/usr/src/linux"
    if os.path.islink(link) or not os.path.exists(link):
        if os.path.islink(link):
            os.remove(link)
        os.symlink(SRC, link)
    msg("reconstruction zfs-kmod")
    run(["emerge", "--quiet-build=y", "-1", "sys-fs/zfs-kmod"])
    run(["depmod", kver])

    # garde-fou : zfs.ko DOIT exister pour CE noyau, sinon l'initramfs partirait
    # sans ZFS et init.py mourrait a l'etape 2 (pas de boot). On verifie avant
    # de construire l'initramfs ET avant d'armer BootNext.
    zko = subprocess.run(["modinfo", "-k", kver, "-n", "zfs"],
                         capture_output=True, text=True).stdout.strip()
    if not zko or not os.path.exists(zko):
        sys.exit(f"ECHEC: zfs.ko absent pour {kver} apres emerge zfs-kmod.\n"
                 f"  -> zfs-kmod ne supporte peut-etre pas encore ce noyau.\n"
                 f"  -> reste sur le noyau actuel, ou essaie ~arch zfs-kmod.\n"
                 f"  (rien n'a ete stage sur l'ESP, BootNext non arme.)")
    msg(f"zfs.ko present : {zko}")

    # 3. modules-<ver>.sfs sur fast_pool/sfs
    subprocess.run(["zfs", "mount", "fast_pool/sfs"], stderr=subprocess.DEVNULL)
    sfs_mnt = out(["zfs", "get", "-H", "-o", "value", "mountpoint",
                   "fast_pool/sfs"]).strip()
    if not os.path.isdir(sfs_mnt):
        sys.exit(f"fast_pool/sfs non monte ({sfs_mnt})")
    sfs_out = os.path.join(sfs_mnt, f"modules-{kver}.sfs")
    msg(f"squashfs modules -> {sfs_out}")
    tmp = sfs_out + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    run(["mksquashfs", f"/lib/modules/{kver}", tmp, "-comp", "zstd",
         "-noappend", "-quiet"])
    os.replace(tmp, sfs_out)

    # 4. initramfs-<ver>.zst
    msg("initramfs")
    env = dict(os.environ, KVER=kver)
    run([sys.executable, BUILD_INITRAMFS], env=env)
    initrd = f"initramfs-{kver}.zst"
    if not os.path.isfile(initrd):
        sys.exit(f"initramfs non produit : {initrd}")

    # 5. staging ESP (anciens conserves)
    dest = os.path.join(ESP, DEST_DIR)
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(SRC, "arch/x86/boot/bzImage"),
                 os.path.join(dest, f"vmlinuz-{kver}.efi"))
    shutil.copy2(initrd, os.path.join(dest, f"initramfs-{kver}.zst"))
    msg(f"stage ESP : vmlinuz-{kver}.efi + initramfs-{kver}.zst")

    # 6. entree EFI versionnee + BootNext
    if not ensure_efivarfs():
        sys.exit("efivarfs indisponible : impossible d'ecrire les variables EFI.\n"
                 "  En chroot, monte-le avant : "
                 "mount -t efivarfs efivarfs /sys/firmware/efi/efivars")
    efi_dir = DEST_DIR.replace("/", "\\")
    label = f"Gentoo-{kver}"
    old = efi_entry(label)
    if old:
        run(["efibootmgr", "-b", old, "-B"])
    run(["efibootmgr", "--create", "--disk", DISK, "--part", PART,
         "--label", label,
         "--loader", f"\\{efi_dir}\\vmlinuz-{kver}.efi",
         "--unicode", f"initrd=\\{efi_dir}\\initramfs-{kver}.zst {CMDLINE}"])
    new = efi_entry(label)
    if not new:
        # diagnostic : montrer ce que efibootmgr voit reellement
        sys.exit("entree EFI introuvable apres creation. Sortie efibootmgr :\n"
                 + out(["efibootmgr"]))
    run(["efibootmgr", "--bootnext", new])
    msg(f"entree {label} = Boot{new} — BootNext arme (essai unique)")

    # indexer la version dans le registre (statut candidate jusqu'au boot valide)
    try:
        import kernel_registry
        reg = kernel_registry.KernelRegistry()
        reg.register(kver,
                     config=os.path.join(SRC, ".config"),
                     modules_sfs=sfs_out,
                     initramfs=os.path.join(dest, f"initramfs-{kver}.zst"),
                     bzimage=os.path.join(dest, f"vmlinuz-{kver}.efi"),
                     efi_entry=new,
                     efi_loader=os.path.join(dest, f"vmlinuz-{kver}.efi"),
                     status=kernel_registry.ST_CANDIDATE)
        msg(f"registre mis a jour : {kver} = candidate")
    except Exception as e:
        msg(f"registre non mis a jour ({e}) -- non bloquant")

    print(f"\nReboote pour tester {kver} :\n"
          f"  boot OK  -> boot_confirm.py promeut {label}\n"
          f"  plantage -> power-cycle : BootNext consomme -> noyau precedent")


if __name__ == "__main__":
    main()

#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
efi_install.py — boot EFI stub direct (sans ZFSBootMenu).
Stage bzImage + initramfs sur l'ESP (FAT32) et cree l'entree efibootmgr.
Le firmware ne lit pas ZFS : le noyau de fast_pool/boot doit etre copie ici.
A lancer en root.
"""
import os
import re
import shutil
import subprocess
import sys

KVER = os.environ.get("KVER", os.uname().release)
ESP = os.environ.get("ESP", "/boot/efi")
DISK = os.environ.get("DISK", "/dev/nvme0n1")
PART = os.environ.get("PART", "1")
KERNEL_SRC = os.environ.get("KERNEL_SRC", f"/mnt/boot/vmlinux-{KVER}")
INITRD_SRC = os.environ.get("INITRD_SRC", f"./initramfs-{KVER}.zst")
LABEL = os.environ.get("LABEL", "Gentoo-Stream")
DEST_DIR = "EFI/gentoo"
CMDLINE = os.environ.get(
    "CMDLINE",
    "i915.force_probe=!4c8b xe.force_probe=4c8b "
    "ip=192.168.1.10::192.168.1.1:255.255.255.0::eth0:off:8.8.8.8 "
    "console=tty0 loglevel=4")


def msg(s):
    print(f">> {s}", flush=True)


def efibootmgr(*args):
    return subprocess.run(["efibootmgr", *args], capture_output=True, text=True)


def existing_entries(label):
    out = efibootmgr().stdout
    return re.findall(rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{re.escape(label)}$",
                      out, re.M)


def main():
    if os.geteuid() != 0:
        sys.exit("root requis")
    if not os.path.ismount(ESP):
        sys.exit(f"ESP non monte sur {ESP}")
    for f in (KERNEL_SRC, INITRD_SRC):
        if not os.path.isfile(f):
            sys.exit(f"introuvable: {f}")

    with open(KERNEL_SRC, "rb") as fh:
        if fh.read(2) != b"MZ":
            msg(f"ATTENTION: {KERNEL_SRC} ne debute pas par 'MZ' (stub EFI ?)")

    dest = os.path.join(ESP, DEST_DIR)
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(KERNEL_SRC, os.path.join(dest, "vmlinuz.efi"))
    shutil.copy2(INITRD_SRC, os.path.join(dest, "initramfs.zst"))
    msg(f"stage ESP : {dest}/{{vmlinuz.efi,initramfs.zst}}")

    efi_dir = DEST_DIR.replace("/", "\\")
    loader = f"\\{efi_dir}\\vmlinuz.efi"
    unicode = f"initrd=\\{efi_dir}\\initramfs.zst {CMDLINE}"

    for num in existing_entries(LABEL):
        efibootmgr("-b", num, "-B")

    efibootmgr("--create", "--disk", DISK, "--part", PART, "--label", LABEL,
               "--loader", loader, "--unicode", unicode)
    msg(f"entree EFI creee : {LABEL}")
    msg("verifie : efibootmgr -v")


if __name__ == "__main__":
    main()

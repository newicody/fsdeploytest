#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
build_initramfs.py — construit l'initramfs (cpio newc + zstd), 100% Python.

Embarque : CPython (interpreteur + stdlib + .so), busybox (shell de secours),
zpool/zfs/mount.zfs + libs, iproute2 (ip), spl.ko/zfs.ko (decompresses),
firmware rtl_nic, les noeuds /dev/console + /dev/null, et init.py -> /init.

A lancer en root. Variables surchargeables par l'environnement.
"""
import os
import glob
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

KVER = os.environ.get("KVER", os.uname().release)
OUT = os.environ.get("OUT", f"initramfs-{KVER}.zst")
INIT_SRC = os.environ.get("INIT_SRC", "./init.py")
FFMPEG_STATIC = os.environ.get("FFMPEG_STATIC", "")
PYBIN = os.environ.get("PYBIN", "/usr/bin/python3")   # python SYSTEME (lancer hors venv)

DIRS = ["bin", "sbin", "etc", "proc", "sys", "dev", "run", "mnt",
        "lib", "lib64", "usr/lib", "usr/bin", "usr/sbin",
        f"lib/modules/{KVER}/extra", "lib/firmware/rtl_nic", "lib/firmware/i915"]

# Firmware a embarquer (motifs glob sous /lib/firmware) :
#   rtl_nic  -> NIC Realtek r8169
#   i915/tgl_* -> GuC + HuC (Rocket Lake reutilise Tiger Lake) -- xe les lit dans i915/
#   i915/rkl_* -> DMC (power management display, specifique Rocket Lake)
# On copie tous les suffixes de version pour ne dependre d'aucun nom precis.
FW_GLOBS = os.environ.get("FW_GLOBS",
                          "/lib/firmware/rtl_nic/rtl8168*:"
                          "/lib/firmware/i915/tgl_*:"
                          "/lib/firmware/i915/rkl_*").split(":")


def msg(s):
    print(f">> {s}", flush=True)


def need_root():
    if os.geteuid() != 0:
        sys.exit("root requis (mknod, depmod)")


def which(name):
    p = shutil.which(name)
    if p:
        return p
    for d in ("/sbin", "/usr/sbin", "/usr/bin", "/bin"):
        c = os.path.join(d, name)
        if os.path.exists(c):
            return c
    return None


def copy(src, stage):
    """Copie src sous stage en CONSERVANT son chemin (nom SONAME), contenu deref.
    Indispensable : le loader cherche libc.so.6, pas libc-2.39.so."""
    dst = stage + src
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(src, dst, follow_symlinks=True)
    return dst


def copy_with_deps(binary, stage):
    src = which(binary) if "/" not in binary else binary
    if not src or not os.path.exists(src):
        msg(f"MANQUANT: {binary}")
        return False
    copy(src, stage)
    try:
        out = subprocess.run(["ldd", src], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    for m in re.finditer(r"(/[^\s]+\.so[^\s]*)", out):
        lib = m.group(1)
        if os.path.exists(lib):
            copy(lib, stage)
    return True


def bundle_python(stage):
    """Interpreteur + stdlib + lib-dynload + libs dynamiques."""
    copy_with_deps(PYBIN, stage)
    # lien stable /usr/bin/python3 -> l'interpreteur reel
    os.makedirs(f"{stage}/usr/bin", exist_ok=True)
    real = os.path.realpath(PYBIN)
    link = f"{stage}/usr/bin/python3"
    if not os.path.exists(link):
        os.symlink(real, link)
    # stdlib complete (inclut lib-dynload : _ctypes, fcntl, etc.)
    stdlib = sysconfig.get_path("stdlib")          # ex: /usr/lib/python3.13
    dst = stage + stdlib
    ignore = shutil.ignore_patterns("test", "tests", "idlelib", "tkinter",
                                    "turtledemo", "ensurepip", "lib2to3",
                                    "config-*", "__pycache__", "*.pyc")
    if not os.path.exists(dst):
        shutil.copytree(stdlib, dst, symlinks=True, ignore=ignore,
                        ignore_dangling_symlinks=True)
    # ldd des extensions compilees (libffi pour _ctypes, etc.)
    dynload = os.path.join(stdlib, "lib-dynload")
    if os.path.isdir(dynload):
        for f in os.listdir(dynload):
            if f.endswith(".so"):
                copy_with_deps(os.path.join(dynload, f), stage)
    msg(f"python embarque ({real}, stdlib {stdlib})")


def bundle_busybox(stage):
    bb = which("busybox")
    if not bb:
        msg("busybox absent (pas de shell de secours)")
        return
    copy_with_deps(bb, stage)
    real = os.path.realpath(bb)
    for ap in ("sh", "mount", "umount", "ls", "cat", "dmesg"):
        link = f"{stage}/bin/{ap}"
        if not os.path.exists(link):
            os.symlink(real, link)
    msg("busybox (secours)")


def bundle_modules(stage):
    """spl.ko + zfs.ko, decompresses si .zst/.xz (finit_module veut du brut)."""
    extra = f"{stage}/lib/modules/{KVER}/extra"
    os.makedirs(extra, exist_ok=True)
    for mod in ("spl", "zfs"):
        try:
            path = subprocess.run(["modinfo", "-k", KVER, "-n", mod],
                                  capture_output=True, text=True).stdout.strip()
        except Exception:
            path = ""
        if not path or not os.path.exists(path):
            msg(f"ATTENTION: module {mod} introuvable pour {KVER}")
            continue
        dst = os.path.join(extra, f"{mod}.ko")
        if path.endswith(".zst"):
            with open(dst, "wb") as o:
                subprocess.run(["zstd", "-d", "-c", path], stdout=o, check=True)
        elif path.endswith(".xz"):
            with open(dst, "wb") as o:
                subprocess.run(["xz", "-d", "-c", path], stdout=o, check=True)
        else:
            shutil.copy2(path, dst)
    # depmod dans le stage
    subprocess.run(["depmod", "-b", stage, KVER], stderr=subprocess.DEVNULL)
    msg("modules zfs/spl (+ depmod)")


def bundle_firmware(stage):
    n = 0
    for pattern in FW_GLOBS:
        for f in glob.glob(pattern):
            if os.path.isfile(f):                # copy() deref les symlinks flottants
                copy(f, stage)
                n += 1
    if n == 0:
        msg("ATTENTION: aucun firmware copie — installe sys-kernel/linux-firmware")
    else:
        msg(f"firmware: {n} blobs (rtl_nic + i915 tgl/rkl pour GuC/HuC/DMC)")


def make_nodes(stage):
    import stat
    console = f"{stage}/dev/console"
    null = f"{stage}/dev/null"
    if not os.path.exists(console):
        os.mknod(console, stat.S_IFCHR | 0o600, os.makedev(5, 1))
    if not os.path.exists(null):
        os.mknod(null, stat.S_IFCHR | 0o666, os.makedev(1, 3))


def pack(stage, out):
    find = subprocess.Popen(["find", ".", "-print0"], cwd=stage,
                            stdout=subprocess.PIPE)
    cpio = subprocess.Popen(["cpio", "--null", "-o", "-H", "newc"],
                            cwd=stage, stdin=find.stdout,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    find.stdout.close()
    with open(out, "wb") as o:
        subprocess.run(["zstd", "-19", "-T0", "-f", "-c"],
                       stdin=cpio.stdout, stdout=o, check=True)
    cpio.stdout.close()


def main():
    need_root()
    stage = tempfile.mkdtemp(prefix="initramfs.")
    msg(f"staging : {stage}")
    try:
        for d in DIRS:
            os.makedirs(os.path.join(stage, d), exist_ok=True)
        make_nodes(stage)

        # loader dynamique
        for ld in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2"):
            if os.path.exists(ld):
                copy(ld, stage)

        bundle_python(stage)
        bundle_busybox(stage)
        for b in ("zpool", "zfs", "mount.zfs", "ip"):
            copy_with_deps(b, stage)
        bundle_modules(stage)
        bundle_firmware(stage)

        if FFMPEG_STATIC and os.access(FFMPEG_STATIC, os.X_OK):
            os.makedirs(f"{stage}/usr/bin", exist_ok=True)
            shutil.copy2(FFMPEG_STATIC, f"{stage}/usr/bin/ffmpeg")
            os.chmod(f"{stage}/usr/bin/ffmpeg", 0o755)
            msg("ffmpeg statique inclus")
        else:
            msg("ffmpeg non inclus -> stream apres switch_root (session_launch)")

        if not os.path.exists(INIT_SRC):
            sys.exit(f"init introuvable: {INIT_SRC}")
        dst_init = f"{stage}/init"
        shutil.copy2(INIT_SRC, dst_init)         # source init.py -> /init
        os.chmod(dst_init, 0o755)
        msg("/init installe (init.py)")

        pack(stage, OUT)
        size = subprocess.run(["du", "-h", OUT], capture_output=True, text=True).stdout.split()[0]
        msg(f"OK -> {OUT}  ({size})")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()

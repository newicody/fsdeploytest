#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
initramfs_verify.py — verifier le contenu d'un initramfs genere, sans booter.

Reutilisable : appele par build_initramfs.py (post-build) ET a la demande
(CLI / autre module). Fait trois choses :
  - liste le contenu de l'image (generateur d'entrees)
  - verifie la presence des fichiers ATTENDUS (/init, zfs.ko, mount.zfs,
    ffmpeg, zfs_load_order, firmware...) -> generateur de constats
  - calcule des sommes de controle SHA-256 (image globale + par fichier) ->
    manifeste enregistrable dans le registre pour controle d'integrite

Format initramfs : cpio (newc) eventuellement compresse zstd/gzip/xz. On
decompresse via l'outil systeme puis on parse le cpio newc en pur Python (pas
de dependance). Aucune extraction sur disque : tout en flux / memoire.

Generateurs partout : on streame les entrees plutot que de tout charger.
Stdlib uniquement (+ zstd/gzip/xz/lz4 system pour la decompression).
"""
import hashlib
import io
import os
import struct
import subprocess
import sys

# fichiers/chemins attendus dans un initramfs de ce projet (motifs simples)
EXPECTED = [
    "init",                                   # /init = PID 1
    "usr/bin/python3",                        # CPython embarque
    "lib/modules",                            # arbre modules (zfs.ko dedans)
    "sbin/mount.zfs",                         # ou usr/sbin : teste les deux
    "bin/busybox",
]
# au moins un de chaque groupe doit etre present (chemins alternatifs)
EXPECTED_ANY = [
    ("zpool", ["sbin/zpool", "usr/sbin/zpool", "bin/zpool"]),
    ("zfs", ["sbin/zfs", "usr/sbin/zfs", "bin/zfs"]),
    ("ip", ["sbin/ip", "usr/sbin/ip", "bin/ip"]),
    ("zfs_load_order", [None]),               # teste par suffixe plus bas
]


# --------------------------------------------------------------------------- #
# decompression -> flux cpio brut
# --------------------------------------------------------------------------- #
def _decompress(path):
    """Retourne les octets cpio decompresses. Detecte le compresseur par magie."""
    with open(path, "rb") as f:
        head = f.read(6)
    tool = None
    if head[:4] == b"\x28\xb5\x2f\xfd":
        tool = ["zstd", "-d", "-c", path]
    elif head[:2] == b"\x1f\x8b":
        tool = ["gzip", "-d", "-c", path]
    elif head[:6] == b"\xfd7zXZ\x00":
        tool = ["xz", "-d", "-c", path]
    elif head[:4] == b"\x04\x22\x4d\x18":
        tool = ["lz4", "-d", "-c", path]
    if tool is None:                          # deja du cpio brut
        with open(path, "rb") as f:
            return f.read()
    p = subprocess.run(tool, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"decompression echouee ({tool[0]}): "
                           f"{p.stderr.decode(errors='replace')[:120]}")
    return p.stdout


# --------------------------------------------------------------------------- #
# parse cpio newc (format SVR4 'newc', magic 070701) -> generateur d'entrees
# --------------------------------------------------------------------------- #
def iter_cpio(data):
    """Genere (name, mode, size, data_bytes) pour chaque entree du cpio newc.
    S'arrete a l'entree de fin 'TRAILER!!!'. Pur Python, en flux memoire."""
    buf = io.BytesIO(data)
    while True:
        header = buf.read(110)
        if len(header) < 110 or header[:6] != b"070701":
            break
        fields = [int(header[6 + i*8: 6 + (i+1)*8], 16) for i in range(13)]
        mode, _, _, _, _, _, _, _, namesize = (
            fields[1], 0, 0, 0, 0, 0, 0, 0, fields[11])
        filesize = fields[6]
        name = buf.read(namesize)
        name = name.rstrip(b"\x00").decode("utf-8", "replace")
        # padding du nom : (110 + namesize) aligne sur 4
        pad = (4 - ((110 + namesize) % 4)) % 4
        buf.read(pad)
        if name == "TRAILER!!!":
            break
        filedata = buf.read(filesize)
        buf.read((4 - (filesize % 4)) % 4)    # padding des donnees
        yield (name, mode, filesize, filedata)


def iter_entries(path):
    """Genere (name, mode, size, data) depuis une image initramfs sur disque."""
    yield from iter_cpio(_decompress(path))


# --------------------------------------------------------------------------- #
# checksums (generateur) + image globale
# --------------------------------------------------------------------------- #
def iter_checksums(path):
    """Genere (name, size, sha256_hex) pour chaque fichier regulier du cpio."""
    for name, mode, size, data in iter_entries(path):
        if size == 0 and not data:
            continue
        # 0o100000 = S_IFREG : on hash les fichiers reguliers
        if mode & 0o170000 == 0o100000:
            yield (name, size, hashlib.sha256(data).hexdigest())


def image_sha256(path):
    """SHA-256 de l'image initramfs complete (le .zst tel quel)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# verification des fichiers attendus (generateur de constats)
# --------------------------------------------------------------------------- #
def verify_contents(path):
    """Genere (niveau, message) : OK / MANQUANT pour chaque attendu. Niveau =
    'crit' (init, zfs, mount.zfs) ou 'warn' (ffmpeg, firmware)."""
    names = set()
    has_zfs_ko = False
    has_load_order = False
    has_ffmpeg = False
    has_firmware = False
    for name, mode, size, _ in iter_entries(path):
        names.add(name)
        if name.endswith("/zfs.ko") or name == "lib/modules" or "/extra/zfs.ko" in name:
            has_zfs_ko = has_zfs_ko or name.endswith("zfs.ko")
        if name.endswith("zfs_load_order"):
            has_load_order = True
        if name.endswith("/ffmpeg") or name == "usr/bin/ffmpeg":
            has_ffmpeg = True
        if "/firmware/" in name or name.startswith("lib/firmware"):
            has_firmware = True

    def present(p):
        return p in names or any(n == p or n.startswith(p + "/") for n in names)

    # critiques
    for p in ("init",):
        yield ("crit", f"{p} : {'OK' if present(p) else 'MANQUANT'}")
    yield ("crit", f"zfs.ko : {'OK' if has_zfs_ko else 'MANQUANT'}")
    yield ("crit", f"zfs_load_order : {'OK' if has_load_order else 'MANQUANT'}")
    for label, alts in (("zpool", ["sbin/zpool", "usr/sbin/zpool", "bin/zpool"]),
                        ("zfs", ["sbin/zfs", "usr/sbin/zfs", "bin/zfs"]),
                        ("mount.zfs", ["sbin/mount.zfs", "usr/sbin/mount.zfs"])):
        ok = any(present(x) for x in alts)
        yield ("crit", f"{label} : {'OK' if ok else 'MANQUANT'}")
    # importants mais non bloquants
    ok_py = any(present(x) for x in ("usr/bin/python3", "bin/python3"))
    yield ("crit", f"python3 : {'OK' if ok_py else 'MANQUANT'}")
    ok_ip = any(present(x) for x in ("sbin/ip", "usr/sbin/ip", "bin/ip"))
    yield ("warn", f"ip : {'OK' if ok_ip else 'MANQUANT'}")
    yield ("warn", f"ffmpeg (stream) : {'OK' if has_ffmpeg else 'absent'}")
    yield ("warn", f"firmware : {'OK' if has_firmware else 'absent'}")


def manifest(path):
    """Construit un manifeste de controle d'integrite (dict serialisable) :
    sha de l'image + sha par fichier + bilan des constats critiques."""
    constats = list(verify_contents(path))
    crit_missing = [m for lvl, m in constats if lvl == "crit" and "MANQUANT" in m]
    files = {name: {"size": size, "sha256": digest}
             for name, size, digest in iter_checksums(path)}
    return {
        "image": os.path.basename(path),
        "image_sha256": image_sha256(path),
        "file_count": len(files),
        "files": files,
        "checks": [m for _, m in constats],
        "critical_missing": crit_missing,
        "bootable": not crit_missing,
    }


# --------------------------------------------------------------------------- #
def main():
    import argparse
    import json
    ap = argparse.ArgumentParser(
        description="verifier le contenu d'un initramfs (sans booter)")
    ap.add_argument("image", help="chemin de l'initramfs (.zst/.cpio...)")
    ap.add_argument("--list", action="store_true", help="lister le contenu")
    ap.add_argument("--sums", action="store_true", help="afficher les checksums")
    ap.add_argument("--manifest", default=None, help="ecrire le manifeste JSON")
    a = ap.parse_args()

    if not os.path.exists(a.image):
        sys.exit(f"introuvable : {a.image}")

    if a.list:
        for name, mode, size, _ in iter_entries(a.image):
            kind = "d" if mode & 0o170000 == 0o040000 else (
                "l" if mode & 0o170000 == 0o120000 else "-")
            print(f"  {kind} {size:>10}  {name}")

    if a.sums:
        for name, size, digest in iter_checksums(a.image):
            print(f"  {digest}  {size:>10}  {name}")

    print("\n=== verification ===")
    bootable = True
    for lvl, msg in verify_contents(a.image):
        flag = "!!" if (lvl == "crit" and "MANQUANT" in msg) else "  "
        print(f" {flag} [{lvl}] {msg}")
        if lvl == "crit" and "MANQUANT" in msg:
            bootable = False
    print(f"\nimage SHA-256 : {image_sha256(a.image)}")
    print(f"verdict : {'BOOTABLE (a priori)' if bootable else 'NON BOOTABLE (manques critiques)'}")

    if a.manifest:
        with open(a.manifest, "w") as f:
            json.dump(manifest(a.image), f, indent=2)
        print(f"manifeste : {a.manifest}")
    sys.exit(0 if bootable else 1)


if __name__ == "__main__":
    main()

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
ESP = os.environ.get("ESP", "/mnt/esp1")     # resolu via boot_layout dans main()
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


def purge_our_entries():
    """Supprime TOUTES les entrees EFI dont le loader pointe vers notre dossier
    (DEST_DIR, ex EFI/gentoo) : nos vmlinuz-*.efi et anciennes UKI. Repart propre
    a chaque build -> pas d'accumulation, pas de melange d'anciennes entrees qui
    pointent vers des fichiers absents ou de mauvaises versions. NE TOUCHE PAS
    aux entrees tierces (Windows, Debian, firmware...)."""
    # efibootmgr -v montre le chemin du loader : File(\EFI\gentoo\vmlinuz-...)
    needle = DEST_DIR.replace("/", "\\").lower()       # ex 'efi\gentoo'
    verbose = out(["efibootmgr", "-v"])
    removed = []
    for line in verbose.splitlines():
        m = re.match(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*)", line)
        if not m:
            continue
        bootnum, rest = m.group(1), m.group(2).lower()
        # supprimer si le loader pointe vers notre dossier
        if needle in rest:
            run(["efibootmgr", "-b", bootnum, "-B"])
            removed.append(bootnum)
    if removed:
        msg(f"purge entrees EFI obsoletes (notre dossier) : {', '.join(removed)}")
    else:
        msg("aucune ancienne entree EFI a purger")
    return removed



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


def resolve_esp():
    """Resout l'ESP primaire via boot_layout/infra.conf (install_mount +
    PARTUUID -> disk/part). Repli sur les env-vars ESP/DISK/PART si l'ini ou
    boot_layout sont absents. Retourne (esp_mount, disk, part)."""
    try:
        import boot_layout
        infra = os.environ.get("INFRA_CONF", "infra.conf")
        esp = boot_layout.primary_esp(infra)
        if esp:
            mp = esp.mount_for_install(log=msg)
            disk, part = esp.disk_and_part()
            if mp and disk:
                msg(f"ESP resolue via infra.conf : {esp.name} -> {mp} "
                    f"(disk={disk} part={part})")
                return mp, disk, part
            msg(f"ESP {esp.name} : resolution incomplete (mp={mp}, disk={disk}) "
                f"-> repli env-vars")
    except Exception as e:
        msg(f"boot_layout indisponible ({e}) -> repli env-vars ESP/DISK/PART")
    return (os.environ.get("ESP", "/mnt/esp1"),
            os.environ.get("DISK", "/dev/nvme0n1"),
            os.environ.get("PART", "1"))


def load_kernel_opts():
    """Lit [kernel] de infra.conf : src, jobs, make_flags, cmdline. Les env-vars
    (SRC/JOBS/CMDLINE) restent PRIORITAIRES pour un override ponctuel. Retourne
    (src, jobs, make_flags_list, cmdline)."""
    src, jobs, flags, cmdline = SRC, JOBS, [], CMDLINE
    try:
        import boot_layout  # apporte load_config indirectement
    except Exception:
        pass
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(os.environ.get("INFRA_CONF", "infra.conf"))
        k = cfg.get("kernel", {})
        if "SRC" not in os.environ and k.get("src"):
            src = k.get("src")
        if "JOBS" not in os.environ and k.get("jobs"):
            jobs = str(k.get("jobs"))
        if "CMDLINE" not in os.environ and k.get("cmdline"):
            cmdline = k.get("cmdline")
        mf = k.get("make_flags", "")
        if isinstance(mf, list):
            flags = [x for x in mf if x]
        elif mf:
            flags = mf.split()
    except Exception as e:
        msg(f"[kernel] non lu ({e}) -> valeurs par defaut/env")
    return src, jobs, flags, cmdline


def main():
    if os.geteuid() != 0:
        sys.exit("root requis")
    global ESP, DISK, PART, SRC, JOBS, CMDLINE
    SRC, JOBS, make_flags, CMDLINE = load_kernel_opts()
    ESP, DISK, PART = resolve_esp()
    if not os.path.ismount(ESP):
        sys.exit(f"ESP non monte sur {ESP} (verifie [efi] dans infra.conf ou "
                 f"monte-la a son install_mount)")
    if not os.path.isfile(os.path.join(SRC, ".config")):
        sys.exit(f".config absent dans {SRC} (lance kernel_watch.py d'abord)")

    kver = out(["make", "-C", SRC, "-s", "kernelrelease"]).strip()
    msg(f"build noyau {kver} (-j{JOBS}"
        + (f" {' '.join(make_flags)}" if make_flags else "") + ")")

    # 1. noyau + modules in-tree
    run(["make", "-C", SRC, f"-j{JOBS}"] + make_flags)
    run(["make", "-C", SRC, "modules_install"] + make_flags)

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

    # 3. modules-<ver>.sfs sur fast_pool/sfs (via le module dedie)
    import sfs_build
    r = sfs_build.build_modules_sfs(kver, "fast_pool/sfs", log=msg, force=True)
    if not r.ok:
        sys.exit(f"creation modules.sfs echouee : {r.reason}")
    sfs_out = r.path

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
    # NETTOYAGE : degager TOUTES nos anciennes entrees (pointant vers DEST_DIR)
    # avant d'en recreer -> repart propre, pas de melange d'anciennes versions.
    purge_our_entries()
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

    # 6bis. Une entree EFI CLASSIQUE par profil (normal/safe/i915), noyau+initrd
    # SEPARES (initrd=\EFI\... passe par le firmware). Methode fiable : pas de
    # section PE a bricoler (l'UKI objcopy ne passait pas l'initrd au noyau ->
    # prepare_namespace -> panic VFS). Chaque profil = sa cmdline ; sur les 2 ESP.
    try:
        import uki_build                       # reutilise load_profiles ([uki])
        infra = os.environ.get("INFRA_CONF", "infra.conf")
        enabled, profiles = uki_build.load_profiles(infra)
        if enabled and profiles:
            # ESP(s) : staging deja fait pour la 1ere ; stager aussi sur la 2e.
            esps = [(ESP, DISK, PART)]
            try:
                import boot_layout
                for e in boot_layout.load_esps(infra):
                    mp = e.mount_for_install(log=msg)
                    d, p = e.disk_and_part()
                    if mp and os.path.abspath(mp) != os.path.abspath(ESP):
                        esps.append((mp, d, p))
            except Exception as e:
                msg(f"boot_layout indisponible ({e}) -> 1 seule ESP")

            efi_dir_bs = DEST_DIR.replace("/", "\\")
            for esp_mnt, disk, part in esps:
                # s'assurer que noyau+initrd sont sur CETTE ESP
                dst_dir = os.path.join(esp_mnt, DEST_DIR)
                os.makedirs(dst_dir, exist_ok=True)
                vm = os.path.join(dst_dir, f"vmlinuz-{kver}.efi")
                it = os.path.join(dst_dir, f"initramfs-{kver}.zst")
                if not os.path.exists(vm):
                    shutil.copy2(os.path.join(dest, f"vmlinuz-{kver}.efi"), vm)
                if not os.path.exists(it):
                    shutil.copy2(os.path.join(dest, f"initramfs-{kver}.zst"), it)
                if not disk:
                    msg(f"  {esp_mnt} : pas de disk/part -> pas d'entree NVRAM "
                        f"(fichiers stages quand meme)")
                    continue
                for prof in profiles:
                    lbl = prof.get("label", prof["name"])
                    cmd = prof.get("cmdline", CMDLINE)
                    old = efi_entry(lbl)
                    if old:
                        run(["efibootmgr", "-b", old, "-B"])
                    run(["efibootmgr", "--create", "--disk", disk, "--part", part,
                         "--label", lbl,
                         "--loader", f"\\{efi_dir_bs}\\vmlinuz-{kver}.efi",
                         "--unicode",
                         f"initrd=\\{efi_dir_bs}\\initramfs-{kver}.zst {cmd}"])
                    msg(f"  entree '{lbl}' -> {disk}p{part} "
                        f"(initrd separe + cmdline profil)")
            msg(f"entrees EFI par profil creees ({len(profiles)} profils "
                f"x {len([e for e in esps if e[1]])} ESP avec NVRAM)")
    except Exception as e:
        msg(f"entrees profils non creees ({e}) -- non bloquant, entree classique OK")

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

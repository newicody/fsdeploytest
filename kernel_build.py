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


def efi_entries_all(label):
    """TOUTES les entrees portant exactement ce label (avec --duplicate, il peut
    y en avoir plusieurs d'anciens builds). Sert a toutes les supprimer avant de
    recreer proprement."""
    return re.findall(
        rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{re.escape(label)}(?:\s|$)",
        out(["efibootmgr"]), re.M)


def _load_boot_opts():
    """Lit [uki] arm_bootnext + default_profile de infra.conf. Retourne
    (arm_bootnext: bool, default_profile: str)."""
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(os.environ.get("INFRA_CONF", "infra.conf"))
        uki = cfg.get("uki", {})
        arm = str(uki.get("arm_bootnext", "false")).lower() in ("true", "1", "yes")
        default = str(uki.get("default_profile", "")).strip()
        return arm, default
    except Exception:
        return False, ""


def set_boot_order_first(bootnum):
    """Place bootnum en TETE du BootOrder (sans supprimer les autres). Le boot
    par defaut devient cette entree."""
    m = re.search(r"^BootOrder:\s*(.+)$", out(["efibootmgr"]), re.M)
    order = [x.strip() for x in m.group(1).split(",")] if m else []
    order = [b for b in order if b.upper() != bootnum.upper()]
    new_order = ",".join([bootnum] + order)
    run(["efibootmgr", "-o", new_order])
    return new_order


def purge_our_entries(keep=None):
    """Supprime les entrees EFI dont le loader pointe vers notre dossier
    (DEST_DIR), SAUF celles fraichement creees (keep = set de Boot####). Appelee
    APRES creation des nouvelles -> supprime uniquement les ANCIENNES orphelines,
    jamais de fenetre sans entrees. NE TOUCHE PAS aux entrees tierces."""
    keep = keep or set()
    needle = DEST_DIR.replace("/", "\\").lower()       # ex 'efi\gentoo'
    verbose = out(["efibootmgr", "-v"])
    removed = []
    for line in verbose.splitlines():
        m = re.match(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*)", line)
        if not m:
            continue
        bootnum, rest = m.group(1), m.group(2).lower()
        if bootnum in keep:
            continue                          # entree qu'on vient de creer : garder
        if needle in rest:
            run(["efibootmgr", "-b", bootnum, "-B"])
            removed.append(bootnum)
    if removed:
        msg(f"purge anciennes entrees EFI (orphelines) : {', '.join(removed)}")
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
    initrd = f"initramfs-{kver}.zst"
    # CRITIQUE : supprimer l'ancien AVANT de rebuild. Sinon, si build_initramfs
    # echoue (auto-test/verify_bootable -> sys.exit), l'ancien fichier reste et
    # serait copie tel quel sur l'ESP -> on booterait un VIEUX initramfs (bug
    # observe : /tmp/pyerr persiste alors que le code est a jour).
    if os.path.exists(initrd):
        os.remove(initrd)
    r = subprocess.run([sys.executable, BUILD_INITRAMFS], env=env)
    if r.returncode != 0:
        sys.exit(f"build_initramfs a ECHOUE (code {r.returncode}). "
                 "L'ESP n'est PAS mise a jour (pas de copie d'un vieux initramfs).")
    if not os.path.isfile(initrd):
        sys.exit(f"initramfs non produit : {initrd} (build_initramfs n'a rien ecrit)")
    # tracer l'horodatage pour confirmer la fraicheur
    import time as _t
    age = _t.time() - os.path.getmtime(initrd)
    msg(f"initramfs produit : {initrd} (il y a {age:.0f}s, "
        f"{os.path.getsize(initrd)//1024} Ko)")

    # 5. staging ESP (anciens conserves)
    dest = os.path.join(ESP, DEST_DIR)
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(SRC, "arch/x86/boot/bzImage"),
                 os.path.join(dest, f"vmlinuz-{kver}.efi"))
    shutil.copy2(initrd, os.path.join(dest, f"initramfs-{kver}.zst"))
    # VERIFICATION : l'initramfs copie sur l'ESP est-il identique a celui produit ?
    import hashlib
    def _md5(p):
        h = hashlib.md5()
        with open(p, "rb") as f:
            for blk in iter(lambda: f.read(65536), b""):
                h.update(blk)
        return h.hexdigest()
    src_sum = _md5(initrd)
    esp_sum = _md5(os.path.join(dest, f"initramfs-{kver}.zst"))
    if src_sum != esp_sum:
        sys.exit(f"copie ESP corrompue : md5 different ({src_sum[:8]} != {esp_sum[:8]})")
    msg(f"stage ESP : vmlinuz-{kver}.efi + initramfs-{kver}.zst (md5 {src_sum[:8]} OK)")

    # 6. entree EFI versionnee + BootNext
    if not ensure_efivarfs():
        sys.exit("efivarfs indisponible : impossible d'ecrire les variables EFI.\n"
                 "  En chroot, monte-le avant : "
                 "mount -t efivarfs efivarfs /sys/firmware/efi/efivars")
    # ORDRE SUR : on CREE d'abord les nouvelles entrees, on purge les vieilles
    # APRES. Ainsi il n'y a JAMAIS de fenetre ou la machine se retrouve sans
    # aucune entree (ce qui arriverait si une purge precoce etait suivie d'un
    # echec de creation). On note les Boot#### qu'on cree pour ne pas les purger.
    created = set()
    efi_dir = DEST_DIR.replace("/", "\\")
    label = f"Gentoo-{kver}"
    for old in efi_entries_all(label):
        run(["efibootmgr", "-b", old, "-B"])
    # --duplicate (-D) : FORCER la creation meme si une entree vers le meme
    # loader existe deja. Sans ca, efibootmgr DEDUPLIQUE sur le path du noyau et
    # REFUSE les profils (qui pointent tous vers le meme vmlinuz, seule la
    # cmdline differe) -> une seule entree creee au lieu de toutes.
    run(["efibootmgr", "--create", "--duplicate", "--disk", DISK, "--part", PART,
         "--label", label,
         "--loader", f"\\{efi_dir}\\vmlinuz-{kver}.efi",
         "--unicode", f"initrd=\\{efi_dir}\\initramfs-{kver}.zst {CMDLINE}"])
    new = efi_entry(label)
    if not new:
        # diagnostic : montrer ce que efibootmgr voit reellement
        sys.exit("entree EFI introuvable apres creation. Sortie efibootmgr :\n"
                 + out(["efibootmgr"]))
    created.add(new)
    # BootNext conditionnel : seulement si [uki] arm_bootnext = true (validation
    # auto d'un nouveau noyau). En phase debug (false), on n'arme RIEN -> le menu
    # BIOS / BootOrder decide, tu choisis librement le profil a tester.
    _arm, _default_prof = _load_boot_opts()
    _default_bootnum = [None]              # mutable pour la boucle profils
    if _arm:
        run(["efibootmgr", "--bootnext", new])
        msg(f"entree {label} = Boot{new} — BootNext arme (essai unique)")
    else:
        msg(f"entree {label} = Boot{new} — BootNext NON arme "
            f"(arm_bootnext=false : choisis le profil dans le menu BIOS)")

    # 6bis. Une entree EFI CLASSIQUE par profil (normal/safe/i915), noyau+initrd
    # SEPARES (initrd=\EFI\... passe par le firmware). Methode fiable : pas de
    # section PE a bricoler (l'UKI objcopy ne passait pas l'initrd au noyau ->
    # prepare_namespace -> panic VFS). Chaque profil = sa cmdline ; sur les 2 ESP.
    try:
        import uki_build                       # reutilise load_profiles ([uki])
        infra = os.environ.get("INFRA_CONF", "infra.conf")
        if not os.path.exists(infra):
            msg(f"  [!] infra.conf INTROUVABLE ({infra}) -> profils NON crees, "
                f"seule l'entree classique existe. Definis INFRA_CONF ou lance "
                f"depuis le bon repertoire.")
        enabled, profiles = uki_build.load_profiles(infra)
        msg(f"  [uki] enabled={enabled}, {len(profiles)} profil(s) lus depuis {infra}")
        if enabled and profiles:
            # ESP(s) : (mount, disk, part, tag, register). La 1ere = celle deja
            # stagee (ESP primaire env). tag = suffixe de label pour distinguer
            # les entrees des 2 disques (ex Gentoo-safe-nvme0 vs -nvme1).
            esps = [(ESP, DISK, PART, os.environ.get("ESP_TAG", "nvme0"), True)]
            try:
                import boot_layout
                for e in boot_layout.load_esps(infra):
                    mp = e.mount_for_install(log=msg)
                    d, p = e.disk_and_part()
                    if mp and os.path.abspath(mp) != os.path.abspath(ESP):
                        esps.append((mp, d, p, e.tag, e.register_uefi))
            except Exception as e:
                msg(f"boot_layout indisponible ({e}) -> 1 seule ESP")

            efi_dir_bs = DEST_DIR.replace("/", "\\")
            for esp_mnt, disk, part, tag, register in esps:
                # s'assurer que noyau+initrd sont sur CETTE ESP. TOUJOURS ecraser
                # (un ancien fichier de meme nom = on booterait du vieux code).
                dst_dir = os.path.join(esp_mnt, DEST_DIR)
                os.makedirs(dst_dir, exist_ok=True)
                vm = os.path.join(dst_dir, f"vmlinuz-{kver}.efi")
                it = os.path.join(dst_dir, f"initramfs-{kver}.zst")
                shutil.copy2(os.path.join(dest, f"vmlinuz-{kver}.efi"), vm)
                shutil.copy2(os.path.join(dest, f"initramfs-{kver}.zst"), it)
                msg(f"  {esp_mnt} : vmlinuz + initramfs ecrases (a jour)")
                if not disk or not register:
                    msg(f"  {esp_mnt} (tag {tag}) : register_uefi=off ou pas de "
                        f"disk -> fichiers stages, PAS d'entree NVRAM")
                    continue
                for prof in profiles:
                    base = prof.get("label", prof["name"])
                    lbl = f"{base}-{tag}"        # ex Gentoo-safe-nvme0
                    cmd = prof.get("cmdline", CMDLINE)
                    old = efi_entries_all(lbl)
                    for o in old:
                        try:
                            run(["efibootmgr", "-b", o, "-B"])
                        except Exception as e:
                            msg(f"  [!] suppression ancienne '{lbl}' : {e}")
                    # creation : capturer l'erreur PAR PROFIL (un echec n'arrete
                    # pas les autres) et MONTRER la vraie sortie efibootmgr.
                    create_cmd = [
                        "efibootmgr", "--create", "--duplicate",
                        "--disk", disk, "--part", part,
                        "--label", lbl,
                        "--loader", f"\\{efi_dir_bs}\\vmlinuz-{kver}.efi",
                        "--unicode",
                        f"initrd=\\{efi_dir_bs}\\initramfs-{kver}.zst {cmd}"]
                    msg("$ " + " ".join(create_cmd[:8]) + " ...")
                    cr = subprocess.run(create_cmd, capture_output=True, text=True)
                    if cr.returncode != 0:
                        msg(f"  [!] ECHEC creation '{lbl}' (code {cr.returncode}) :")
                        msg(f"      stderr: {cr.stderr.strip()[:200]}")
                        msg(f"      stdout: {cr.stdout.strip()[:200]}")
                        continue                  # passer au profil suivant
                    bn = efi_entry(lbl)
                    if bn:
                        created.add(bn)
                        if prof["name"] == _default_prof and _default_bootnum[0] is None:
                            _default_bootnum[0] = bn
                        msg(f"  OK entree '{lbl}' = Boot{bn} -> {disk}p{part}")
                    else:
                        msg(f"  [!] '{lbl}' creee mais introuvable apres "
                            f"(parsing efi_entry ?) -- sortie efibootmgr -v :")
                        for ln in out(["efibootmgr"]).splitlines():
                            if lbl in ln:
                                msg(f"      {ln}")
            msg(f"entrees EFI par profil creees ({len(profiles)} profils "
                f"x {len([e for e in esps if e[1]])} ESP avec NVRAM)")
    except Exception as e:
        msg(f"entrees profils non creees ({e}) -- non bloquant, entree classique OK")

    # PURGE FINALE : maintenant que TOUTES les nouvelles entrees existent, on
    # supprime les ANCIENNES orphelines (en preservant celles qu'on vient de
    # creer). Jamais de fenetre sans entrees -> en cas d'echec plus haut, les
    # vieilles entrees restent (la machine garde de quoi booter).
    purge_our_entries(keep=created)

    # BOOTORDER : placer le profil par defaut (ex 'safe') en tete -> boot par
    # defaut sur le profil le plus sur si tu ne touches a rien dans le BIOS.
    if _default_bootnum[0]:
        order = set_boot_order_first(_default_bootnum[0])
        msg(f"BootOrder : profil '{_default_prof}' (Boot{_default_bootnum[0]}) "
            f"en tete -> {order}")
    elif _default_prof:
        msg(f"default_profile='{_default_prof}' non trouve parmi les entrees "
            f"creees -> BootOrder inchange")

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

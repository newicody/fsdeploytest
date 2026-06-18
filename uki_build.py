#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
uki_build.py — construit des UKI multi-profils (sans dependance systemd).

UKI = Unified Kernel Image : un binaire EFI unique bundlant vmlinuz + initramfs
+ cmdline. Comme le noyau est compile avec CONFIG_EFI_STUB=y, le vmlinuz EST
deja un binaire EFI (PE) : on greffe les sections .cmdline / .initrd / .osrel
directement dessus via objcopy. AUCUNE dependance (pas de stub systemd).

Chaque profil de [uki] (infra.conf) -> 1 UKI :
  - place sur les 2 ESP (chaque disque bootable seul),
  - entree efibootmgr si register_uefi,
  - ecrit aussi \\EFI\\BOOT\\BOOTX64.EFI si fallback (bootable SANS NVRAM).

Secure Boot DESACTIVE (UKI non signes) -- signature ajoutable plus tard.
Retour observable (UkiResult), pas d'exception qui fuit. ASCII-only.
"""
import os
import shutil
import subprocess


class UkiResult:
    __slots__ = ("ok", "name", "path", "reason")

    def __init__(self, ok, name="", path="", reason=""):
        self.ok = bool(ok)
        self.name = name
        self.path = path
        self.reason = reason

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return (f"<UkiResult {self.name} {'ok' if self.ok else 'FAIL'} "
                f"{self.path} {self.reason}>")


def _sh(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr)
    except FileNotFoundError:
        return 127, f"{cmd[0]} introuvable"


def _pe_section_end(vmlinuz):
    """Trouve l'adresse virtuelle la plus haute deja occupee dans le vmlinuz PE,
    pour placer nos sections APRES (sans chevauchement). Utilise objdump -h.
    Retourne une adresse alignee (0x10000) ou un defaut sur si echec."""
    rc, out = _sh(["objdump", "-h", vmlinuz])
    top = 0
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            # format objdump -h : idx name size VMA LMA fileoff algn
            if len(parts) >= 6 and parts[0].isdigit():
                try:
                    size = int(parts[2], 16)
                    vma = int(parts[3], 16)
                    end = vma + size
                    if end > top:
                        top = end
                except ValueError:
                    continue
    if top == 0:
        top = 0x3000000          # defaut sur (48 Mo) si objdump muet
    # alignement 64 Ko
    return (top + 0xffff) & ~0xffff


def build_uki(vmlinuz, initramfs, cmdline, out_path, osrel=None, log=print):
    """Greffe .cmdline/.initrd/.osrel sur une COPIE du vmlinuz -> out_path.
    Retourne UkiResult. N'altere jamais le vmlinuz d'origine."""
    if not os.path.exists(vmlinuz):
        return UkiResult(False, os.path.basename(out_path), out_path,
                         f"vmlinuz absent : {vmlinuz}")
    if not os.path.exists(initramfs):
        return UkiResult(False, os.path.basename(out_path), out_path,
                         f"initramfs absent : {initramfs}")
    # fichiers temporaires pour les sections texte
    work = out_path + ".work"
    cmdline_file = out_path + ".cmdline"
    osrel_file = out_path + ".osrel"
    try:
        with open(cmdline_file, "w") as f:
            f.write(cmdline.strip() + "\n")
        with open(osrel_file, "w") as f:
            f.write(osrel or 'NAME="Gentoo UKI"\nID=gentoo\n')

        base = _pe_section_end(vmlinuz)
        # adresses successives, espacees large pour eviter tout chevauchement
        a_osrel = base
        a_cmdline = base + 0x20000
        a_initrd = base + 0x40000

        cmd = ["objcopy",
               "--add-section", f".osrel={osrel_file}",
               "--change-section-vma", f".osrel={hex(a_osrel)}",
               "--add-section", f".cmdline={cmdline_file}",
               "--change-section-vma", f".cmdline={hex(a_cmdline)}",
               "--add-section", f".initrd={initramfs}",
               "--change-section-vma", f".initrd={hex(a_initrd)}",
               vmlinuz, work]
        log(f"  objcopy UKI {os.path.basename(out_path)} "
            f"(initrd@{hex(a_initrd)})")
        rc, out = _sh(cmd)
        if rc != 0:
            return UkiResult(False, os.path.basename(out_path), out_path,
                             f"objcopy: {out.strip()[:120]}")
        os.replace(work, out_path)
        size_mb = os.path.getsize(out_path) / (1024 * 1024)
        log(f"  OK {out_path} ({size_mb:.0f} Mo)")
        return UkiResult(True, os.path.basename(out_path), out_path)
    except OSError as e:
        return UkiResult(False, os.path.basename(out_path), out_path, str(e))
    finally:
        for f in (cmdline_file, osrel_file, work):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass


def _copy_to_esp(uki_path, esp_mnt, subdir, name, log):
    """Copie un UKI vers <esp>/EFI/<subdir>/<name>. Retourne le chemin ou None."""
    dst_dir = os.path.join(esp_mnt, "EFI", subdir)
    try:
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name)
        shutil.copy2(uki_path, dst)
        log(f"    -> {dst}")
        return dst
    except OSError as e:
        log(f"    [!] copie ESP echouee ({e})")
        return None


def deploy_profiles(profiles, vmlinuz, initramfs, esps, log=print,
                    build_dir="/tmp", efibootmgr_fn=None):
    """Construit et deploie tous les profils UKI.
      profiles : liste de dicts {name,label,cmdline,register_uefi,fallback}
      esps     : liste de (mountpoint_esp, disk, part) pour chaque ESP
      efibootmgr_fn : callback(label, disk, part, loader_path) pour creer
                      l'entree UEFI (injecte par kernel_build, qui gere efivarfs)
    Retourne la liste des UkiResult."""
    results = []
    for prof in profiles:
        name = prof["name"]
        uki_name = f"{name}.efi"
        staged = os.path.join(build_dir, uki_name)
        r = build_uki(vmlinuz, initramfs, prof["cmdline"], staged,
                      osrel=f'NAME="Gentoo {prof.get("label", name)}"\n'
                            f'ID=gentoo\nPRETTY_NAME="{prof.get("label", name)}"\n',
                      log=log)
        if not r.ok:
            results.append(r)
            log(f"  profil {name} ECHEC : {r.reason}")
            continue
        # deployer sur chaque ESP
        for esp_mnt, disk, part in esps:
            dst = _copy_to_esp(staged, esp_mnt, "gentoo", uki_name, log)
            if dst and prof.get("fallback"):
                # \EFI\BOOT\BOOTX64.EFI : bootable sans NVRAM (secours autonome)
                _copy_to_esp(staged, esp_mnt, "BOOT", "BOOTX64.EFI", log)
        # entree efibootmgr (sur le 1er disque) si demande
        if prof.get("register_uefi") and efibootmgr_fn and esps:
            _, disk0, part0 = esps[0]
            loader = f"\\EFI\\gentoo\\{uki_name}"
            try:
                efibootmgr_fn(prof.get("label", name), disk0, part0, loader)
                log(f"  entree UEFI '{prof.get('label', name)}' -> {loader}")
            except Exception as e:
                log(f"  [!] entree UEFI '{name}' echouee : {e}")
        try:
            os.remove(staged)
        except OSError:
            pass
        results.append(r)
    return results


def load_profiles(infra_conf="infra.conf"):
    """Lit les profils [uki] de infra.conf (configobj dispo cote rootfs/chroot).
    Retourne (enabled, [profils])."""
    try:
        from configobj import ConfigObj
    except ImportError:
        return False, []
    cfg = ConfigObj(infra_conf)
    sect = cfg.get("uki", {})
    if not sect:
        return False, []
    enabled = str(sect.get("enabled", "false")).lower() in ("true", "1", "yes")
    profiles = []
    for name, decl in sect.items():
        if not isinstance(decl, dict):
            continue
        profiles.append({
            "name": name,
            "label": decl.get("label", name),
            "cmdline": decl.get("cmdline", ""),
            "register_uefi": str(decl.get("register_uefi", "false")).lower()
                             in ("true", "1", "yes"),
            "fallback": str(decl.get("fallback", "false")).lower()
                        in ("true", "1", "yes"),
        })
    return enabled, profiles


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="construit des UKI multi-profils")
    ap.add_argument("--vmlinuz", required=True)
    ap.add_argument("--initramfs", required=True)
    ap.add_argument("--infra", default="infra.conf")
    ap.add_argument("--esp", action="append", default=[],
                    help="point de montage d'une ESP (repetable)")
    ap.add_argument("--build-dir", default="/tmp")
    a = ap.parse_args()
    enabled, profiles = load_profiles(a.infra)
    if not enabled:
        raise SystemExit("[uki] enabled=false ou section absente")
    esps = [(mnt, "", "") for mnt in a.esp] or [("/boot/efi", "", "")]
    res = deploy_profiles(profiles, a.vmlinuz, a.initramfs, esps,
                          build_dir=a.build_dir)
    ok = sum(1 for r in res if r.ok)
    print(f"\n{ok}/{len(res)} UKI construits")
    for r in res:
        print(" ", repr(r))
    raise SystemExit(0 if ok == len(res) else 1)

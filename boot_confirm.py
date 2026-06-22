#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
boot_confirm.py — Brique 2bis : valide le noyau frais boote et le PROMEUT.
A executer apres boot dans le nouveau noyau (service OpenRC en runlevel
default, ou depuis session_launch). Garde-fou :
  - sante OK    -> promotion en tete de BootOrder (devient defaut)
  - sante KO    -> pas de promotion (reboot = fallback)
  - panic/hang  -> ce script ne tourne pas ; BootNext consomme -> noyau precedent
"""
import os
import re
import subprocess
import sys

POOL = os.environ.get("POOL", "fast_pool")
KVER = os.uname().release
LABEL = f"Gentoo-{KVER}"


def log(s):
    print(f"[boot-confirm] {s}", flush=True)


def _align_manager_root():
    """Aligne MANAGER_ROOT sur infra.conf [manager] (env > config), comme
    first_boot/operate, pour que la promotion ecrive dans le MEME manager.
    Best-effort : si configobj/infra absent, on garde l'env/defaut."""
    if "MANAGER_ROOT" in os.environ:
        return
    inf = os.environ.get("INFRA_CONF", "/etc/infra.conf")
    try:
        from configobj import ConfigObj
        mr = (ConfigObj(inf).get("manager", {}) or {}).get("root")
        if mr:
            os.environ["MANAGER_ROOT"] = mr
    except Exception:
        pass


def out(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def healthy():
    ok = True
    if subprocess.run(["zpool", "list", POOL],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode != 0:
        log(f"pool {POOL} absent")
        ok = False
    if "default" not in out(["ip", "route"]):
        log("pas de route par defaut")
        ok = False
    # stream vivant (decommente pour l'exiger) :
    # if not any(p in out(["pgrep", "-l", "ffmpeg"]) for p in ("ffmpeg",)): ok = False
    return ok


def entry_num(label):
    m = re.findall(rf"^Boot([0-9A-Fa-f]{{4}})\*?\s+{re.escape(label)}$",
                   out(["efibootmgr"]), re.M)
    return m[0] if m else None


def boot_order():
    m = re.search(r"^BootOrder:\s*(.+)$", out(["efibootmgr"]), re.M)
    return m.group(1).strip().split(",") if m else []


def main():
    _align_manager_root()
    if not healthy():
        log(f"HEALTH-CHECK ECHEC pour {KVER} — pas de promotion.")
        log("Un reboot repartira sur le noyau precedent (BootOrder inchange).")
        sys.exit(1)

    num = entry_num(LABEL)
    if not num:
        log(f"entree EFI '{LABEL}' introuvable")
        sys.exit(1)

    order = [num] + [e for e in boot_order() if e != num]
    subprocess.run(["efibootmgr", "-o", ",".join(order)],
                   stdout=subprocess.DEVNULL)
    log(f"PROMU : {LABEL} (Boot{num}) en tete de BootOrder ({','.join(order)})")

    # registre : kver -> current, ancienne current -> fallback
    try:
        import kernel_registry
        reg = kernel_registry.KernelRegistry()
        if KVER in reg:
            reg.promote(KVER)
            log(f"registre : {KVER} = current, anciennes -> fallback")
    except Exception as e:
        log(f"registre non mis a jour ({e}) -- non bloquant")

    log(f"OK — {KVER} est desormais le noyau par defaut.")


if __name__ == "__main__":
    main()

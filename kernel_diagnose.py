#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
kernel_diagnose.py — diagnostic de coherence au demarrage (etape 3-4).

NON bloquant : lit, croise, rapporte. Ne modifie rien, ne quitte jamais en
erreur a cause d'une anomalie detectee (seul un probleme d'execution interne
peut faire echouer le script).

Croise trois sources :
  - la .config du noyau (courant ou cible)
  - l'etat runtime : lsmod (modules charges) + dmesg (messages noyau)
  - health.json produit par init.py (etat ZFS/disques/memoire au boot)

Deux niveaux :
  1. regles deterministes (toujours fiables, sans LLM)
  2. synthese LLM optionnelle (--llm) : si l'endpoint est down, on saute
     proprement, le niveau 1 suffit.

Sortie : rapport texte sur stdout + JSON (--json <path>) pour l'inference.

Usage :
  python3 kernel_diagnose.py
  python3 kernel_diagnose.py --config /usr/src/linux/.config --llm
"""
import argparse
import json
import os
import re
import subprocess
import sys

import config_delta

DEF_CONFIG = None                 # auto : /proc/config.gz, /boot/config-*, src
DEF_SRC = "/usr/src/linux"
DEF_HEALTH = "/etc/health.json"   # ecrit par init.py sous NEWROOT
DEF_ENDPOINT = "http://127.0.0.1:11434/v1"
DEF_MODEL = "qwen3:30b"

# niveaux de severite
CRIT, WARN, INFO = "CRITIQUE", "ATTENTION", "INFO"


# --------------------------------------------------------------------------- #
# regles deterministes : config seule
# --------------------------------------------------------------------------- #
# (symbole, valeur_requise, severite, explication)
#   valeur_requise : "y" | "m" | "ym" (y ou m) | "not-y" (tout sauf y)
REQUIRED = [
    ("CONFIG_EFI_STUB",     "y",  CRIT, "boot EFI direct impossible sans stub"),
    ("CONFIG_BINFMT_SCRIPT","y",  CRIT, "/init (shebang python) ne se lance pas"),
    ("CONFIG_RD_ZSTD",      "y",  CRIT, "initramfs .zst non decompressable"),
    ("CONFIG_BLK_DEV_LOOP", "y",  CRIT, "montage loop des .sfs impossible"),
    ("CONFIG_SQUASHFS",     "y",  CRIT, "rootfs.sfs/modules.sfs non montables"),
    ("CONFIG_SQUASHFS_ZSTD","y",  CRIT, "squashfs zstd non lisible"),
    ("CONFIG_SQUASHFS_XATTR","y", WARN, "xattr (ACL/caps/SELinux) ignores au montage"),
    ("CONFIG_OVERLAY_FS",   "y",  CRIT, "overlay rootfs impossible"),
    ("CONFIG_BLK_DEV_NVME", "y",  CRIT, "disques NVMe invisibles"),
    ("CONFIG_R8169",        "y",  WARN, "NIC Realtek : reseau precoce compromis si =m"),
    ("CONFIG_REALTEK_PHY",  "y",  WARN, "PHY Realtek : lien reseau possiblement absent"),
    ("CONFIG_IP_PNP",       "y",  WARN, "config IP par cmdline (ip=) inoperante"),
    ("CONFIG_FW_LOADER",    "y",  CRIT, "firmware GuC/HuC/rtl_nic non chargeable"),
    ("CONFIG_DRM",          "y",  CRIT, "aucun affichage"),
]
# au moins un des deux pilotes GPU
GPU_DRIVERS = ["CONFIG_DRM_XE", "CONFIG_DRM_I915"]
# interdits en =y (licence)
FORBIDDEN_Y = [
    ("CONFIG_ZFS", "CDDL : ZFS doit rester module, jamais =y"),
    ("CONFIG_SPL", "CDDL : SPL doit rester module, jamais =y"),
]


def _ok(value, requirement):
    if requirement == "y":
        return value == "y"
    if requirement == "m":
        return value == "m"
    if requirement == "ym":
        return value in ("y", "m")
    if requirement == "not-y":
        return value != "y"
    return False


def check_config(cfg):
    """cfg : dict {SYMBOL: val}. Retourne liste de (severite, sym, message)."""
    issues = []
    for sym, req, sev, why in REQUIRED:
        val = cfg.get(sym, "n")
        if not _ok(val, req):
            issues.append((sev, sym, f"={val}, attendu {req} -- {why}"))
    if not any(cfg.get(s) in ("y", "m") for s in GPU_DRIVERS):
        issues.append((CRIT, "DRM_XE/DRM_I915",
                       "aucun pilote GPU actif (xe ni i915)"))
    for sym, why in FORBIDDEN_Y:
        if cfg.get(sym) == "y":
            issues.append((CRIT, sym, f"=y interdit -- {why}"))
    return issues


# --------------------------------------------------------------------------- #
# etat runtime : lsmod + dmesg
# --------------------------------------------------------------------------- #
def loaded_modules():
    try:
        out = subprocess.run(["lsmod"], text=True, capture_output=True).stdout
    except OSError:
        return set()
    mods = set()
    for line in out.splitlines()[1:]:
        name = line.split(None, 1)[0] if line.split() else ""
        if name:
            mods.add(name)
    return mods


def dmesg_text():
    try:
        return subprocess.run(["dmesg"], text=True, capture_output=True).stdout
    except OSError:
        return ""


# motifs dmesg interessants (motif, severite, etiquette)
DMESG_PATTERNS = [
    (r"i915.*([Ff]irmware).*(failed|not found|missing)", WARN, "firmware i915/xe manquant"),
    (r"xe.*([Ff]irmware).*(failed|not found|missing)",    WARN, "firmware xe manquant"),
    (r"r8169.*([Ll]ink (is )?down)",                       WARN, "lien reseau r8169 absent"),
    (r"(Hardware Error|mce:|Machine check)",               CRIT, "erreur materielle (MCE)"),
    (r"(I/O error|critical medium error)",                 CRIT, "erreur disque"),
    (r"(Out of memory|oom-kill)",                          WARN, "pression memoire (OOM)"),
    (r"zfs.*([Ee]rror|failed)",                            WARN, "anomalie ZFS"),
]


def check_runtime(cfg, mods, dmesg):
    issues = []

    # coherence config <-> modules charges
    want_xe = cfg.get("CONFIG_DRM_XE") in ("y", "m")
    if want_xe and "xe" not in mods and "i915" in mods:
        issues.append((WARN, "xe",
                       "DRM_XE active mais 'xe' non charge et 'i915' present "
                       "-- bascule force_probe (xe.force_probe=4c8b) non prise ?"))
    if cfg.get("CONFIG_ZFS") in ("y", "m") and "zfs" not in mods:
        issues.append((WARN, "zfs",
                       "ZFS configure mais module 'zfs' non liste par lsmod"))

    # signaux dmesg
    for pat, sev, label in DMESG_PATTERNS:
        m = re.search(pat, dmesg, re.IGNORECASE)
        if m:
            ctx = m.group(0).strip()[:100]
            issues.append((sev, "dmesg", f"{label} : {ctx}"))
    return issues


# --------------------------------------------------------------------------- #
# health.json (init.py)
# --------------------------------------------------------------------------- #
def check_health(path):
    issues = []
    if not os.path.exists(path):
        return [(INFO, "health", f"rapport de boot absent ({path})")]
    try:
        h = json.loads(open(path).read())
    except (OSError, json.JSONDecodeError) as e:
        return [(WARN, "health", f"rapport de boot illisible ({e})")]
    st = (h.get("pool_state") or "").upper()
    if st == "DEGRADED":
        issues.append((WARN, "zpool", f"pool {h.get('pool')} DEGRADED au boot"))
    elif st in ("FAULTED", "UNAVAIL"):
        issues.append((CRIT, "zpool", f"pool {h.get('pool')} {st} au boot"))
    if not h.get("memory_ok", True):
        for m in h.get("memory_msgs", []):
            issues.append((WARN, "memoire", m))
    return issues


# --------------------------------------------------------------------------- #
# config auto-detection
# --------------------------------------------------------------------------- #
def find_config(explicit, src):
    if explicit:
        return explicit
    if os.path.exists("/proc/config.gz"):
        return "/proc/config.gz"
    rel = os.uname().release
    if os.path.exists(f"/boot/config-{rel}"):
        return f"/boot/config-{rel}"
    sc = os.path.join(src, ".config")
    return sc if os.path.exists(sc) else None


# --------------------------------------------------------------------------- #
# LLM (optionnel)
# --------------------------------------------------------------------------- #
SYSTEM_DIAG = (
    "Tu es un assistant de diagnostic noyau Linux pour une appliance ZFS/stream "
    "(Intel Rocket Lake, pilote xe via force_probe, ZFS en module, rootfs "
    "squashfs+overlay, reseau r8169). On te donne les anomalies deterministes "
    "deja detectees + des extraits dmesg. Indique en 3-5 puces : autres "
    "anomalies probables, et quoi prioriser. Sois bref et concret."
)


def llm_summary(endpoint, model, issues, dmesg_excerpt):
    import urllib.error
    import urllib.request
    text = "\n".join(f"[{s}] {k}: {m}" for s, k, m in issues) or "(aucune)"
    user = (f"Anomalies deterministes :\n{text}\n\n"
            f"Extrait dmesg (fin) :\n{dmesg_excerpt[-1500:]}\n")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_DIAG},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                 data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
        return data["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError,
            json.JSONDecodeError, TimeoutError) as e:
        return f"(synthese LLM indisponible : {e})"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
SEV_ORDER = {CRIT: 0, WARN: 1, INFO: 2}


def main():
    ap = argparse.ArgumentParser(description="diagnostic de coherence au demarrage")
    ap.add_argument("--config", default=DEF_CONFIG)
    ap.add_argument("--src", default=DEF_SRC)
    ap.add_argument("--health", default=DEF_HEALTH)
    ap.add_argument("--json", default=None, help="ecrit le rapport JSON ici")
    ap.add_argument("--llm", action="store_true", help="ajouter une synthese LLM")
    ap.add_argument("--endpoint", default=DEF_ENDPOINT)
    ap.add_argument("--model", default=DEF_MODEL)
    a = ap.parse_args()

    cfg_path = find_config(a.config, a.src)
    issues = []
    if not cfg_path:
        issues.append((WARN, "config", "aucune .config trouvee"))
        cfg = {}
    else:
        cfg = config_delta.parse_config(cfg_path)
        issues += check_config(cfg)

    mods = loaded_modules()
    dmesg = dmesg_text()
    issues += check_runtime(cfg, mods, dmesg)
    issues += check_health(a.health)

    issues.sort(key=lambda it: SEV_ORDER.get(it[0], 9))

    # rapport texte
    print("=== Diagnostic de coherence (non bloquant) ===")
    print(f"config : {cfg_path or 'introuvable'}")
    n_crit = sum(1 for s, _, _ in issues if s == CRIT)
    n_warn = sum(1 for s, _, _ in issues if s == WARN)
    if not issues:
        print("aucune anomalie detectee.")
    for sev, key, msg in issues:
        print(f"  [{sev}] {key}: {msg}")
    print(f"\nbilan : {n_crit} critique(s), {n_warn} attention(s).")

    llm_text = None
    if a.llm:
        print("\n--- synthese LLM ---")
        llm_text = llm_summary(a.endpoint, a.model, issues, dmesg)
        print(llm_text)

    if a.json:
        report = {
            "config": cfg_path,
            "issues": [{"severity": s, "key": k, "message": m}
                       for s, k, m in issues],
            "critical": n_crit, "warnings": n_warn,
            "llm_summary": llm_text,
        }
        try:
            with open(a.json, "w") as f:
                json.dump(report, f, indent=2)
            print(f"\nrapport JSON : {a.json}")
        except OSError as e:
            print(f"!! JSON non ecrit ({e})", file=sys.stderr)

    # jamais bloquant : sortie 0 quoi qu'il arrive
    sys.exit(0)


if __name__ == "__main__":
    main()

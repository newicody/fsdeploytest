#!/usr/bin/env python3
"""
kernel-watch.py — Brique 1 du systeme d'auto-update (NOYAU UNIQUEMENT).

Pipeline :
  1. detecte une version de noyau plus recente dans les tags git de l'arbre
  2. extrait les NOUVEAUX symboles Kconfig (make listnewconfig = facon oldconfig)
  3. demande a un LLM local (endpoint OpenAI-compatible : llama.cpp server OU
     OpenVINO Model Server) une reco y/m/n + raison pour chaque symbole
  4. VALIDATION LOCALE interactive
  5. ecrit un fragment + merge_config.sh + make olddefconfig, montre le diff

NE COMPILE PAS, NE BOOTE PAS, NE PUBLIE RIEN (briques suivantes).
Sans dependance externe (urllib + subprocess).
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, urllib.error, urllib.request
from pathlib import Path

import config_delta

DEF_SRC      = "/usr/src/linux"
DEF_ENDPOINT = "http://127.0.0.1:11434/v1"    # Ollama (API OpenAI-compatible)
DEF_MODEL    = "qwen3:30b"                     # tag Ollama (verifier `ollama list`)
VER_RE       = re.compile(r"(\d+)\.(\d+)\.(\d+)")

SYSTEM = (
    "Tu configures un noyau Linux pour une appliance precise : Intel Rocket Lake, "
    "iGPU UHD730 pilote par xe (force_probe=4c8b, i915 en repli), NIC Realtek r8169 "
    "en dur avec REALTEK_PHY en dur, ZFS en module hors-arbre, rootfs squashfs+overlay, "
    "boot EFI-stub direct, stream framebuffer puis wayland vers YouTube. "
    "Pour chaque nouveau symbole Kconfig, propose 'y', 'm' ou 'n' avec UNE phrase de raison. "
    "Conserve le materiel qui sert (xe, i915, r8169/realtek, squashfs, overlay, loop, efi, drm, vaapi). "
    "Desactive ce qui est clairement hors-scope. "
    "Reponds STRICTEMENT par un tableau JSON d'objets "
    '{"symbol":"CONFIG_X","value":"y|m|n","reason":"..."} et RIEN d\'autre.'
)

def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)

def ver_key(s: str):
    m = VER_RE.search(s)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

def list_tags(src: str):
    return [t for t in run(["git", "-C", src, "tag", "--list"]).stdout.split() if t]

def newer_versions(src: str, current: str):
    cur = ver_key(current)
    found = {t for t in list_tags(src) if ver_key(t) > cur}
    return sorted(found, key=ver_key)

def new_symbols(src: str, config: str):
    """make listnewconfig -> liste des symboles Kconfig nouveaux/non definis."""
    env = dict(os.environ, KCONFIG_CONFIG=config)
    r = run(["make", "-C", src, "listnewconfig"], env=env)
    raw = r.stdout.strip()
    syms = [l.strip() for l in raw.splitlines() if l.strip().startswith("CONFIG_")]
    return syms, raw

def llm_chat(endpoint: str, model: str, system: str, user: str, max_tokens=4096):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        raise RuntimeError(f"endpoint LLM inaccessible ({endpoint}): {e.reason}") from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"reponse LLM inattendue: {e}") from e
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"reponse LLM inattendue (pas de 'choices'): {data}") from e

def parse_json_array(text: str):
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("aucun tableau JSON dans la reponse")
    return json.loads(text[i:j + 1])

def propose(endpoint, model, raw):
    user = ("Nouveaux symboles Kconfig (sortie de `make listnewconfig`) :\n\n"
            + raw + "\n\nDonne le tableau JSON de recommandations.")
    out = llm_chat(endpoint, model, SYSTEM, user)
    try:
        return parse_json_array(out)
    except Exception:
        print("!! reponse LLM non parsable :\n" + out, file=sys.stderr)
        raise

def approve(props):
    print("\n=== Propositions — validation locale ([Y]/n/e) ===")
    chosen = []
    for p in props:
        sym = p.get("symbol", "?")
        val = p.get("value", "?")
        reason = p.get("reason", "")
        ans = input(f"  [{val}] {sym} — {reason}\n      appliquer ? ").strip().lower()
        if ans in ("", "y", "o"):
            chosen.append((sym, val, reason))
        elif ans == "e":
            nv = (input("      valeur (y/m/n) : ").strip() or val)
            chosen.append((sym, nv, reason + " [valeur ajustee manuellement]"))
    return chosen

def write_fragment(chosen, path):
    out = []
    for entry in chosen:
        sym, val = entry[0], entry[1]
        base = sym if sym.startswith("CONFIG_") else "CONFIG_" + sym
        out.append(f"# {base} is not set" if val == "n" else f"{base}={val}")
    Path(path).write_text("\n".join(out) + "\n")
    return path

def apply_fragment(src, config, fragment):
    """Merge le fragment validE, PUIS montre exactement ce qu'olddefconfig
    resout/change tout seul avant d'enteriner (point aveugle comble).
    Retourne le ConfigDelta complet (etat initial -> etat final entérine)."""
    before_all = config_delta.parse_config(config)
    merge = Path(src) / "scripts" / "kconfig" / "merge_config.sh"
    if merge.exists():
        run([str(merge), "-m", config, fragment], cwd=src)
    else:
        print("!! merge_config.sh absent — fragment non fusionne", file=sys.stderr)

    # olddefconfig sur COPIE d'abord : on regarde ce qu'il change seul
    try:
        delta, resolved = config_delta.diff_around_olddefconfig(src, config)
    except RuntimeError as e:
        print(f"!! {e}")
        sys.exit(1)

    if delta:
        print("\n=== olddefconfig modifierait AUSSI (resolution auto) ===")
        print(config_delta.summarize(delta))
        # signaler specifiquement les retraits/desactivations : potentiellement
        # une dependance cassee par le patch -> a verifier
        risky = delta["disabled"] + delta["removed"]
        if risky:
            print("\n!! attention, desactivations/retraits silencieux ci-dessus "
                  "(dependance Kconfig possiblement cassee par le patch).")
        ans = input("appliquer cette resolution ? [Y/n] ").strip().lower()
        if ans not in ("", "y", "o"):
            print("resolution refusee — .config inchange.")
            sys.exit(0)
    # enteriner : olddefconfig pour de vrai sur l'original
    run(["make", "-C", src, "olddefconfig"],
        env=dict(os.environ, KCONFIG_CONFIG=config))

    after_all = config_delta.parse_config(config)
    return config_delta.diff_configs(before_all, after_all)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEF_SRC)
    ap.add_argument("--config", default=None, help="defaut: <src>/.config")
    ap.add_argument("--endpoint", default=DEF_ENDPOINT)
    ap.add_argument("--model", default=DEF_MODEL)
    ap.add_argument("--fragment", default="/tmp/proposed.config")
    ap.add_argument("--history", default="/boot_pool/manager/config-history",
                    help="dossier parent d'archivage des configs validees "
                         "(boot_pool = durable ; PAS fast_pool qui est un stripe)")
    ap.add_argument("--force", action="store_true",
                    help="sauter le check de version (traiter les symboles courants)")
    a = ap.parse_args()
    config = a.config or str(Path(a.src) / ".config")

    cur = os.uname().release
    print(f"noyau courant : {cur}")
    if not a.force:
        nv = newer_versions(a.src, cur)
        if not nv:
            print("aucune version plus recente dans les tags git — rien a faire.")
            return
        print("plus recentes :", ", ".join(nv))

    syms, raw = new_symbols(a.src, config)
    if not syms:
        print("aucun nouveau symbole Kconfig.")
        return
    print(f"{len(syms)} nouveaux symboles — consultation du LLM ({a.model})...")

    try:
        props = propose(a.endpoint, a.model, raw)
    except RuntimeError as e:
        print(f"!! {e}")
        print("   -> aucune modification du .config (rien n'a ete touche).")
        print("   verifie qu'Ollama tourne : rc-service ollama status / "
              f"curl {a.endpoint}/models")
        sys.exit(1)
    chosen = approve(props)
    if not chosen:
        print("rien de selectionne.")
        return

    frag = write_fragment(chosen, a.fragment)
    print(f"fragment ecrit : {frag}")
    delta = apply_fragment(a.src, config, frag)

    # --- archivage historique (avant compilation) ---------------------------
    import config_history
    reasons = {e[0] if e[0].startswith("CONFIG_") else "CONFIG_" + e[0]: e[2]
               for e in chosen if len(e) > 2}
    kr = run(["make", "-C", a.src, "-s", "kernelrelease"],
             env=dict(os.environ, KCONFIG_CONFIG=config))
    kver = (kr.stdout.strip().splitlines() or [cur])[-1] if kr.returncode == 0 else cur
    try:
        dest = config_history.record(a.history, kver, config, delta, reasons,
                                     src=a.src)
        svg = config_history.render_graph(a.history)
        idx = config_history.render_index(a.history)
        print(f"\nhistorique archive : {dest}")
        print(f"  doc/raisons : {dest}/doc.md")
        print(f"  graphe      : {svg}")
        print(f"  index       : {idx}")
    except OSError as e:
        print(f"!! archivage historique echoue ({e})")

    d = run(["git", "-C", a.src, "diff", "--", ".config"])
    print("\n=== diff .config ===\n" + (d.stdout or "(pas de diff git — verifie a la main)"))
    print("\nOK. Compilation / modules.sfs / ESP / BootNext = briques suivantes.")

if __name__ == "__main__":
    main()

#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
config_history.py — historique versionne des configurations noyau.

A chaque generation de .config validee (avant compilation), on :
  1. archive la .config dans un dossier parent : <hist>/<kver>/.config
  2. ecrit le delta categorise + la raison LLM de chaque changement (delta.json)
  3. documente chaque symbole change (texte d'aide Kconfig si trouvable) (doc.md)
  4. (re)genere un graphe SVG du volume de changements par noyau (stdlib pur)
     + un index Markdown listant tous les noyaux et leurs changements.

Aucune dependance hors stdlib (pas de matplotlib : SVG genere a la main, pour
rester compatible avec une appliance headless sans pile graphique).

API principale :
  record(hist_dir, kver, config_path, delta, reasons, src=None)
  render_graph(hist_dir)          -> <hist>/changes.svg
  render_index(hist_dir)          -> <hist>/index.md
"""
import html
import json
import os
import re
import shutil
import time
from pathlib import Path

# ordre stable des categories (aligne sur config_delta.ConfigDelta)
CATS = ("enabled", "disabled", "switched", "added", "removed")
CAT_COLOR = {
    "enabled":  "#3a7",
    "disabled": "#c54",
    "switched": "#39c",
    "added":    "#7b3",
    "removed":  "#999",
}


# --------------------------------------------------------------------------- #
# doc Kconfig
# --------------------------------------------------------------------------- #
def kconfig_help(src, symbol):
    """Cherche le bloc d'aide d'un symbole dans les Kconfig de l'arbre noyau.
    Retourne une chaine (peut etre vide). Heuristique : grep le 'config NAME'
    puis collecte les lignes 'help' indentees suivantes. Best-effort."""
    if not src:
        return ""
    name = symbol[len("CONFIG_"):] if symbol.startswith("CONFIG_") else symbol
    # recherche limitee : on s'appuie sur grep -r pour la vitesse
    try:
        import subprocess
        r = subprocess.run(
            ["grep", "-rl", "-E", rf"^\s*config {re.escape(name)}\b", str(src),
             "--include=Kconfig*"],
            text=True, capture_output=True, timeout=30)
        files = [f for f in r.stdout.splitlines() if f]
    except Exception:
        return ""
    for f in files[:3]:
        txt = _extract_help(f, name)
        if txt:
            return txt
    return ""


def _extract_help(kfile, name):
    try:
        lines = Path(kfile).read_text(errors="replace").splitlines()
    except OSError:
        return ""
    out, grabbing, in_help, base_indent = [], False, False, None
    pat = re.compile(rf"^\s*config {re.escape(name)}\b")
    for line in lines:
        if not grabbing:
            if pat.match(line):
                grabbing = True
            continue
        stripped = line.strip()
        if re.match(r"^\s*config \w+", line):     # symbole suivant -> stop
            break
        if not in_help:
            if stripped in ("help", "---help---"):
                in_help = True
            continue
        # dans l'aide : lignes indentees
        if stripped == "":
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent
        if indent < base_indent and stripped:
            break
        out.append(stripped)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- #
# enregistrement
# --------------------------------------------------------------------------- #
def record(hist_dir, kver, config_path, delta, reasons=None, src=None):
    """Archive la config + ecrit delta.json + doc.md pour <kver>.
    delta   : config_delta.ConfigDelta
    reasons : {symbol: raison_LLM}
    src     : arbre noyau (pour extraire l'aide Kconfig), optionnel.
    Retourne le dossier cree."""
    reasons = reasons or {}
    dest = Path(hist_dir) / kver
    dest.mkdir(parents=True, exist_ok=True)

    shutil.copy2(config_path, dest / ".config")

    # delta.json structure (iterable -> liste de tuples)
    entries = []
    for cat, sym, before, after in delta:
        entries.append({
            "category": cat, "symbol": sym,
            "before": before, "after": after,
            "reason": reasons.get(sym, ""),
        })
    counts = {c: len(delta[c]) for c in CATS}
    payload = {
        "kver": kver,
        "timestamp": int(time.time()),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "total": sum(counts.values()),
        "changes": entries,
    }
    (dest / "delta.json").write_text(json.dumps(payload, indent=2))

    # doc.md : un paragraphe par changement (raison LLM + aide Kconfig)
    md = [f"# Noyau {kver}", "", f"_Genere le {payload['date']}_", "",
          f"**{payload['total']} changement(s)** : " +
          ", ".join(f"{c}={counts[c]}" for c in CATS if counts[c]), ""]
    for e in entries:
        md.append(f"## {e['symbol']}  (`{e['before']}` -> `{e['after']}`, "
                  f"{e['category']})")
        if e["reason"]:
            md.append(f"- Raison : {e['reason']}")
        help_txt = kconfig_help(src, e["symbol"]) if src else ""
        if help_txt:
            md.append("- Kconfig :")
            for ln in help_txt.splitlines():
                md.append(f"  > {ln}" if ln else "  >")
        md.append("")
    (dest / "doc.md").write_text("\n".join(md))
    return str(dest)


# --------------------------------------------------------------------------- #
# lecture de l'historique
# --------------------------------------------------------------------------- #
def _load_all(hist_dir):
    out = []
    base = Path(hist_dir)
    if not base.is_dir():
        return out
    for d in base.iterdir():
        j = d / "delta.json"
        if j.is_file():
            try:
                out.append(json.loads(j.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
    out.sort(key=lambda p: p.get("timestamp", 0))
    return out


# --------------------------------------------------------------------------- #
# graphe SVG (barres empilees, stdlib pur)
# --------------------------------------------------------------------------- #
def render_graph(hist_dir, out_path=None):
    data = _load_all(hist_dir)
    out_path = out_path or str(Path(hist_dir) / "changes.svg")
    if not data:
        Path(out_path).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="60">'
            '<text x="10" y="35">aucun historique</text></svg>')
        return out_path

    W, H = 720, 360
    pad_l, pad_b, pad_t, pad_r = 50, 80, 30, 20
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b
    n = len(data)
    bw = max(8, min(60, int(plot_w / max(n, 1) * 0.7)))
    gap = plot_w / max(n, 1)
    max_total = max((d["total"] for d in data), default=1) or 1

    def y(v):
        return pad_t + plot_h - (v / max_total) * plot_h

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
             f'height="{H}" font-family="sans-serif" font-size="11">']
    parts.append(f'<text x="{pad_l}" y="18" font-size="14" '
                 f'font-weight="bold">Changements de config par noyau</text>')
    # axe Y (graduations)
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        val = round(max_total * frac)
        yy = y(val)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{W-pad_r}" '
                     f'y2="{yy:.1f}" stroke="#eee"/>')
        parts.append(f'<text x="{pad_l-6}" y="{yy+3:.1f}" text-anchor="end" '
                     f'fill="#666">{val}</text>')
    # barres empilees
    for i, d in enumerate(data):
        cx = pad_l + gap * i + (gap - bw) / 2
        y_cursor = pad_t + plot_h
        for cat in CATS:
            c = d["counts"].get(cat, 0)
            if not c:
                continue
            h = (c / max_total) * plot_h
            y_cursor -= h
            parts.append(f'<rect x="{cx:.1f}" y="{y_cursor:.1f}" width="{bw}" '
                         f'height="{h:.1f}" fill="{CAT_COLOR[cat]}">'
                         f'<title>{cat}: {c}</title></rect>')
        # total au-dessus
        parts.append(f'<text x="{cx+bw/2:.1f}" y="{y_cursor-4:.1f}" '
                     f'text-anchor="middle" fill="#333">{d["total"]}</text>')
        # label kver (tronque, pivote)
        label = html.escape(d["kver"])[:18]
        lx, ly = cx + bw / 2, pad_t + plot_h + 12
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="end" '
                     f'fill="#444" transform="rotate(-45 {lx:.1f} {ly:.1f})">'
                     f'{label}</text>')
    # legende
    lx = pad_l
    for cat in CATS:
        parts.append(f'<rect x="{lx}" y="{H-22}" width="11" height="11" '
                     f'fill="{CAT_COLOR[cat]}"/>')
        parts.append(f'<text x="{lx+15}" y="{H-12}" fill="#444">{cat}</text>')
        lx += 16 + 9 * len(cat) + 14
    parts.append("</svg>")
    Path(out_path).write_text("\n".join(parts))
    return out_path


# --------------------------------------------------------------------------- #
# index Markdown
# --------------------------------------------------------------------------- #
def render_index(hist_dir, out_path=None):
    data = _load_all(hist_dir)
    out_path = out_path or str(Path(hist_dir) / "index.md")
    lines = ["# Historique des configurations noyau", "",
             "![changements](changes.svg)", "",
             "| Date | Noyau | Total | Detail |",
             "|------|-------|-------|--------|"]
    for d in reversed(data):                       # plus recent en haut
        detail = ", ".join(f"{c} {d['counts'][c]}"
                            for c in CATS if d["counts"].get(c))
        lines.append(f"| {d['date']} | `{d['kver']}` | {d['total']} | "
                     f"{detail} | ([doc]({d['kver']}/doc.md)) |")
    Path(out_path).write_text("\n".join(lines) + "\n")
    return out_path

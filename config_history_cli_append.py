#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ===========================================================================
# A APPENDER A LA FIN de config_history.py (qui est aujourd'hui une bibliotheque
# SANS main : la collecte se fait dans kernel_watch via record()). Ce bloc lui
# donne la CLI qui manquait -> 'operate config-history list|render|show' devient
# fonctionnel (c'etait le seul cablage incomplet d'operate).
#
# Path/json/CATS/render_graph/render_index sont deja au niveau module de
# config_history.py -> aucun import a ajouter en tete. Defaut du dossier aligne
# sur kernel_watch (<MANAGER_ROOT>/config-history ; boot_pool durable).
# ===========================================================================
def _default_hist():
    import os
    return os.path.join(os.environ.get("MANAGER_ROOT", "/boot_pool/manager"),
                        "config-history")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="historique des configs noyau (rendu + consultation)")
    ap.add_argument("--hist", default=None,
                    help="dossier d'historique (defaut <MANAGER_ROOT>/config-history)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("render")
    ps = sub.add_parser("show"); ps.add_argument("kver")
    a = ap.parse_args()
    hist = a.hist or _default_hist()
    base = Path(hist)

    if a.cmd == "list":
        if not base.is_dir():
            print(f"(aucun historique sous {hist})"); return 0
        rows = sorted(d.name for d in base.iterdir()
                      if d.is_dir() and (d / "delta.json").exists())
        if not rows:
            print(f"(aucune config archivee sous {hist})"); return 0
        for kv in rows:
            try:
                delta = json.loads((base / kv / "delta.json").read_text())
                n = sum(len(delta.get(c, [])) for c in CATS)
            except Exception:
                n = "?"
            print(f"  {kv:18} {n} changement(s)")
        return 0

    if a.cmd == "render":
        try:
            print(f"graphe : {render_graph(hist)}")
            print(f"index  : {render_index(hist)}")
            return 0
        except Exception as e:
            print(f"!! rendu impossible ({e})"); return 1

    if a.cmd == "show":
        doc = base / a.kver / "doc.md"
        if doc.exists():
            print(doc.read_text()); return 0
        print(f"(pas de doc.md pour {a.kver} sous {hist})"); return 1


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)

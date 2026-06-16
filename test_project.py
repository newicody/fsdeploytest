#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
test_project.py — valider le pont GitHub Projects v2 REEL (GraphQL), isolement.

Ne teste QUE la couche Project : resoudre l'ID du Project, lire le champ Status
et ses options (colonnes), lister les items, creer un draft item de test, le
deplacer de colonne (set status), relire. Aucune compilation/boot/inference.

Prerequis :
  export GITHUB_TOKEN=...     (scope 'project' classic, ou Projects read/write)
  # le Project doit avoir un champ single-select 'Status' avec les options :
  #   Idea, WIP, Dev, Prod, Drop   (sinon ajuste STATUS_OPTION dans github_project)
  python3 test_project.py --owner newicody --number 1

Chaque etape rapporte ok/echec sans laisser fuiter d'exception. Le draft item
de test reste dans le Project (supprime-le a la main, ou via --no-create pour ne
rien creer et juste verifier lecture).
"""
import argparse
import os
import sys
import time

import github_project as gp


def run_step(label, fn):
    try:
        val = fn()
        ok, detail = True, "" if val is None else str(val)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}"
          + (f" -> {detail}" if detail else ""), flush=True)
    return ok, detail


def main():
    ap = argparse.ArgumentParser(description="test isole du pont Projects v2")
    ap.add_argument("--owner", required=True, help="user ou org proprietaire")
    ap.add_argument("--number", required=True, type=int, help="numero du Project")
    ap.add_argument("--token", default=None)
    ap.add_argument("--no-create", action="store_true",
                    help="ne teste que la lecture (pas de draft item cree)")
    a = ap.parse_args()

    token = a.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN absent (scope 'project').")

    print(f">> test Projects v2 sur {a.owner} #{a.number}\n")
    try:
        tp = gp.ProjectV2Transport(token=token)
        board = gp.ProjectBoard(tp, a.owner, a.number)
    except Exception as e:
        sys.exit(f"init echouee : {e}")

    results = []
    st = {"pid": None, "field": None, "opts": None, "item": None}

    # 1) resoudre l'ID du Project (auth + acces)
    def _pid():
        st["pid"] = board.project_id
        return f"id = {st['pid'][:18]}..."
    results.append(run_step("resoudre l'ID du Project", _pid))
    if st["pid"] is None:
        return _end(results)

    # 2) lire le champ Status + options (colonnes)
    def _field():
        st["field"], st["opts"] = tp.status_field(st["pid"])
        manquantes = [v.lower() for v in gp.STATUS_OPTION.values()
                      if v.lower() not in st["opts"]]
        if manquantes:
            raise RuntimeError(f"options manquantes dans Status : {manquantes} "
                               f"(presentes : {list(st['opts'])})")
        return f"champ Status OK, options : {list(st['opts'])}"
    results.append(run_step("lire le champ Status + options", _field))

    # 3) lister les items (generateur -> on compte)
    results.append(run_step(
        "lister les items du Project",
        lambda: f"{sum(1 for _ in board.items())} item(s)"))

    if a.no_create:
        return _end(results)

    # 4) creer un draft item de test
    def _create():
        st["item"] = tp.add_draft(st["pid"],
                                  f"[test-projet] {time.strftime('%H:%M:%S')}",
                                  "Item de test (test_project.py). Sans effet.")
        return f"item {st['item'][:18]}..."
    results.append(run_step("creer un draft item de test", _create))

    # 5) le deplacer en colonne Dev (set status) + relire
    if st["item"] and st["field"]:
        def _move():
            board.set_status(st["item"], gp.brainstorm.S_DEV)
            for it in board.items():
                if it["item_id"] == st["item"]:
                    if it["status"] != gp.brainstorm.S_DEV:
                        raise RuntimeError(f"statut relu = {it['status']}")
                    return "deplace en 'Dev' et relu"
            raise RuntimeError("item introuvable apres deplacement")
        results.append(run_step("deplacer en colonne Dev + relire", _move))

    return _end(results)


def _end(results):
    ok = sum(1 for r, _ in results if r)
    print(f"\n=== bilan : {ok}/{len(results)} etapes OK ===")
    if ok == len(results):
        print("Pont Projects v2 VALIDE. La cinematique board projet est "
              "operationnelle (colonne + label).")
    else:
        print("INCOMPLET. Verifie : scope 'project' du token ; le Project a "
              "bien un champ single-select 'Status' avec Idea/WIP/Dev/Prod/Drop ; "
              "owner (user vs org) et numero corrects. Note : les requetes "
              "GraphQL peuvent demander un ajustement de nom de champ.")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()

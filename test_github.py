#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
test_github.py — valider le pont git (GitHubTransport reel) AVANT tout le reste.

Ne teste QUE la couche transport, isolement : creer une Issue de test, la
relire, commenter, poser/lire un label d'etat, puis la refermer. Aucune
compilation, aucun boot, aucune inference. Le seul effet de bord est une Issue
de test dans ton repo (refermee a la fin, ou laissee ouverte avec --keep).

Prerequis :
  export GITHUB_TOKEN=ghp_xxx        (scope 'repo' : issues read/write)
  python3 test_github.py --repo owner/nom

Etapes (chacune rapporte ok/echec via Outcome, sans laisser fuiter d'exception) :
  1) lister les issues       (verifie auth + acces repo)
  2) creer une Issue de test (verifie write)
  3) la relire par numero    (verifie read)
  4) ajouter un commentaire
  5) poser le label state:dev puis le relire (mapping etat <-> label)
  6) refermer l'Issue        (sauf --keep)

Sortie : un bilan clair. Code retour 0 si tout est vert, 1 sinon.
"""
import argparse
import os
import sys
import time

import github_board as gb


# --- petit Outcome local (meme esprit que first_boot, sans dependance) ------ #
class R:
    __slots__ = ("ok", "label", "detail")

    def __init__(self, ok, label, detail=""):
        self.ok = bool(ok)
        self.label = label
        self.detail = detail

    def __bool__(self):
        return self.ok


def run_step(label, fn):
    """Execute fn, capture la frontiere, renvoie R(ok/echec). fn renvoie une
    valeur (=> ok) ou leve (=> echec). Affiche au fur et a mesure."""
    try:
        val = fn()
        r = R(True, label, "" if val is None else str(val))
    except Exception as e:
        r = R(False, label, f"{type(e).__name__}: {e}")
    mark = "OK  " if r.ok else "FAIL"
    extra = f" -> {r.detail}" if r.detail else ""
    print(f"  [{mark}] {label}{extra}", flush=True)
    return r


def main():
    ap = argparse.ArgumentParser(description="test isole du pont GitHub")
    ap.add_argument("--repo", required=True, help="owner/nom du depot")
    ap.add_argument("--token", default=None, help="sinon GITHUB_TOKEN")
    ap.add_argument("--keep", action="store_true",
                    help="ne pas refermer l'Issue de test")
    a = ap.parse_args()

    token = a.token or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN absent (export GITHUB_TOKEN=... ou --token).")

    print(f">> test du pont GitHub sur {a.repo}\n")
    results = []
    state = {"number": None}

    # transport reel
    try:
        tp = gb.GitHubTransport(a.repo, token=token)
    except Exception as e:
        sys.exit(f"init transport echouee : {e}")

    # 1) lister (auth + acces)
    results.append(run_step(
        "lister les issues (auth + acces repo)",
        lambda: f"{len(tp.list_issues())} issue(s) visible(s)"))

    # 2) creer une Issue de test
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    def _create():
        res = tp.create_issue(
            title=f"[test-pont] verification GitHubTransport {stamp}",
            body="Issue de test creee par test_github.py. "
                 "Sans effet sur le systeme. Peut etre refermee.",
            labels=[gb.STATE_LABELS[gb.brainstorm.S_IDEA]])
        state["number"] = res["number"]
        return f"Issue #{res['number']} creee"
    results.append(run_step("creer une Issue de test", _create))

    if state["number"] is None:
        _summary(results)
        sys.exit(1)                       # inutile de continuer sans Issue
    n = state["number"]

    # 3) relire
    results.append(run_step(
        "relire l'Issue par numero",
        lambda: f"titre = {tp.get_issue(n).get('title','?')[:48]}"))

    # 4) commenter
    results.append(run_step(
        "ajouter un commentaire",
        lambda: tp.add_comment(n, "Commentaire de test (pont OK).") and "poste"))

    # 5) poser + relire un label d'etat
    def _label():
        cur = tp.get_issue(n)
        labels = [l["name"] if isinstance(l, dict) else l
                  for l in cur.get("labels", [])]
        labels = [l for l in labels if not l.startswith("state:")]
        labels.append(gb.STATE_LABELS[gb.brainstorm.S_DEV])
        tp.update_issue(n, labels=labels)
        back = tp.get_issue(n)
        got = gb._state_from_labels(back.get("labels", []))
        if got != gb.brainstorm.S_DEV:
            raise RuntimeError(f"label relu = {got}, attendu dev")
        return "state:dev pose et relu"
    results.append(run_step("poser/relire le label state:dev", _label))

    # 6) refermer
    if a.keep:
        print("  [SKIP] fermeture (--keep) : Issue laissee ouverte")
    else:
        results.append(run_step(
            "refermer l'Issue de test",
            lambda: tp.update_issue(n, state="closed") and "fermee"))

    _summary(results)
    sys.exit(0 if all(r.ok for r in results) else 1)


def _summary(results):
    ok = sum(1 for r in results if r.ok)
    print(f"\n=== bilan : {ok}/{len(results)} etapes OK ===")
    if ok == len(results):
        print("Pont GitHub VALIDE. Tu peux tester Board.push + watch_once, "
              "puis first_boot.py.")
    else:
        print("Pont GitHub INCOMPLET. Verifie : scope du token (repo/issues), "
              "nom du repo (owner/nom), droits d'ecriture.")
        for r in results:
            if not r.ok:
                print(f"  - echec: {r.label} : {r.detail}")


if __name__ == "__main__":
    main()

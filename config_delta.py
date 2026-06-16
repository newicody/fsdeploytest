#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
config_delta.py — outils de comparaison de configurations Kconfig.

Comble le trou de kernel_watch.py : `make listnewconfig` ne voit QUE les
symboles nouveaux. Un patch / une montee de version peut aussi :
  - exiger qu'une option existante change de valeur (m -> y, n -> y...)
  - rendre une option invalide (dependance Kconfig cassee) -> olddefconfig
    la retire SILENCIEUSEMENT
  - retirer un symbole disparu en amont (ligne morte dans ta .config)

Ce module fournit :
  parse_config(path)              -> {SYMBOL: 'y'|'m'|'n'}
  diff_configs(before, after)     -> ConfigDelta (categorise les changements)
  diff_around_olddefconfig(...)   -> ce qu'olddefconfig change tout seul
  summarize(delta)                -> texte lisible (et promptable au LLM)

Aucune dependance hors stdlib.
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_SET_RE   = re.compile(r"^(CONFIG_\w+)=(.*)$")
_UNSET_RE = re.compile(r"^# (CONFIG_\w+) is not set$")


def parse_config(path):
    """Lit une .config (ou .gz) -> {SYMBOL: 'y'|'m'|'n'}.
    Les valeurs non-booleennes (chaines, nombres) sont gardees telles quelles."""
    opener = open
    mode = "rt"
    if str(path).endswith(".gz"):
        import gzip
        opener = gzip.open
    state = {}
    with opener(path, mode, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = _SET_RE.match(line)
            if m:
                state[m.group(1)] = m.group(2)
                continue
            m = _UNSET_RE.match(line)
            if m:
                state[m.group(1)] = "n"
    return state


def _val(state, sym):
    return state.get(sym, "n")


class ConfigDelta:
    """Resultat structure d'une comparaison before -> after.

    Categories (chaque entree = (symbol, before, after)) :
      enabled   : etait n/absent, devient y ou m
      disabled  : etait y/m, devient n
      switched  : y <-> m (ou changement de valeur non-booleenne)
      added     : symbole absent de before, present (et != n) dans after
      removed   : symbole present dans before, totalement absent de after
    """
    __slots__ = ("enabled", "disabled", "switched", "added", "removed")

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, [])

    def __bool__(self):
        return any(getattr(self, s) for s in self.__slots__)

    def __len__(self):
        return sum(len(getattr(self, s)) for s in self.__slots__)

    def __iter__(self):
        """Itere (categorie, symbol, before, after) -- pratique avec yield."""
        for cat in self.__slots__:
            for entry in getattr(self, cat):
                yield (cat, *entry)

    def __getitem__(self, cat):
        if cat not in self.__slots__:
            raise KeyError(
                f"categorie inconnue '{cat}' ; attendu une de {self.__slots__}")
        return getattr(self, cat)

    def get(self, cat, default=None):
        return getattr(self, cat) if cat in self.__slots__ else default


def diff_configs(before, after):
    """before, after : dicts (ou chemins .config). Retourne ConfigDelta."""
    if not isinstance(before, dict):
        before = parse_config(before)
    if not isinstance(after, dict):
        after = parse_config(after)

    d = ConfigDelta()
    keys = set(before) | set(after)
    for sym in sorted(keys):
        b = before.get(sym)            # None = absent
        a = after.get(sym)
        if b == a:
            continue
        bn = b if b is not None else "n"
        an = a if a is not None else "n"

        if b is None and an != "n":
            d.added.append((sym, "absent", an))
        elif a is None:
            d.removed.append((sym, bn, "absent"))
        elif bn == "n" and an in ("y", "m"):
            d.enabled.append((sym, bn, an))
        elif bn in ("y", "m") and an == "n":
            d.disabled.append((sym, bn, an))
        else:
            d.switched.append((sym, bn, an))
    return d


def diff_around_olddefconfig(src, config, make="make"):
    """Capture config, lance `make olddefconfig` sur une COPIE, recapture, et
    retourne (delta, new_config_path) SANS toucher l'original.

    delta = ce qu'olddefconfig a change tout seul (le point aveugle actuel).
    new_config_path = la .config resolue (a valider avant de remplacer l'orig).
    """
    config = str(config)
    before = parse_config(config)

    tmpdir = tempfile.mkdtemp(prefix="cfgdelta_")
    work = os.path.join(tmpdir, ".config")
    shutil.copy2(config, work)

    env = dict(os.environ, KCONFIG_CONFIG=work)
    proc = subprocess.run([make, "-C", src, "olddefconfig"],
                          env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "olddefconfig a echoue:\n" + (proc.stderr or proc.stdout))

    after = parse_config(work)
    return diff_configs(before, after), work


def summarize(delta, max_per_cat=40):
    """Texte lisible (et utilisable comme contexte LLM)."""
    labels = {
        "enabled":  "ACTIVEES (n -> y/m)",
        "disabled": "DESACTIVEES (y/m -> n)",
        "switched": "MODIFIEES (y<->m / valeur)",
        "added":    "AJOUTEES",
        "removed":  "RETIREES (symbole disparu)",
    }
    if not delta:
        return "aucun changement de configuration."
    lines = []
    for cat in ConfigDelta.__slots__:
        entries = delta[cat]
        if not entries:
            continue
        lines.append(f"{labels[cat]} ({len(entries)}):")
        for sym, b, a in entries[:max_per_cat]:
            lines.append(f"  {sym}: {b} -> {a}")
        if len(entries) > max_per_cat:
            lines.append(f"  ... (+{len(entries) - max_per_cat})")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="compare deux .config Kconfig")
    ap.add_argument("before")
    ap.add_argument("after")
    a = ap.parse_args()
    print(summarize(diff_configs(a.before, a.after)))

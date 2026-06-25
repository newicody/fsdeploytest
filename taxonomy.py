#!/usr/bin/env python3
"""taxonomy.py -- chargement de la taxonomie des modes (taxonomy.conf, configobj).

Source unique pour : axes transversaux, cycle de vie (states), modes + leurs
domaines. Consommee par projects.classify (validation + extraction des labels
axis:/domain:) et par les handlers. Volontairement PERMISSIVE : un label inconnu
n'est pas rejete, il est juste signale (valid=False) -> on affine sans bloquer.
"""
import os

_CACHE = {}


def _path(path=None):
    for c in (path, os.environ.get("TAXONOMY_CONF"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxonomy.conf"),
              "/etc/taxonomy.conf"):
        if c and os.path.isfile(c):
            return c
    return None


def load(path=None):
    """Charge (et cache) la taxonomie. Retourne un dict structure ; vide si absent."""
    p = _path(path)
    if not p:
        return {"axes": [], "states": [], "machine_transitions": {}, "modes": {}}
    if p in _CACHE:
        return _CACHE[p]
    try:
        from configobj import ConfigObj
        c = ConfigObj(p)
    except Exception:
        return {"axes": [], "states": [], "machine_transitions": {}, "modes": {}}

    def _csv(v):
        if v is None:
            return []
        return v if isinstance(v, list) else [x.strip() for x in v.split(",") if x.strip()]

    mt = {}
    for pair in _csv((c.get("states", {}) or {}).get("machine_transitions")):
        if ":" in pair:
            a, b = pair.split(":", 1)
            mt[a.strip()] = b.strip()

    modes = {}
    for name, sub in (c.get("modes", {}) or {}).items():
        if isinstance(sub, dict):
            modes[name] = {
                "entity": (sub.get("entity") or "").strip(),
                "domains": _csv(sub.get("domains")),
            }

    out = {
        "axes": _csv((c.get("axes", {}) or {}).get("list")),
        "states": _csv((c.get("states", {}) or {}).get("list")),
        "machine_transitions": mt,
        "modes": modes,
    }
    _CACHE[p] = out
    return out


def axes(path=None):
    return load(path)["axes"]


def states(path=None):
    return load(path)["states"]


def modes(path=None):
    return list(load(path)["modes"].keys())


def domains(mode, path=None):
    return (load(path)["modes"].get(mode, {}) or {}).get("domains", [])


def machine_next(state, path=None):
    """state suivant que la MACHINE peut poser (ou None). Ex 'idea' -> 'explore'."""
    return load(path)["machine_transitions"].get(state)


def validate(mode="", axis="", domain="", state="", path=None):
    """Retourne {mode,axis,domain,state}_ok (True/False ; True si vide = non specifie)."""
    t = load(path)
    md = t["modes"].get(mode, {})
    return {
        "mode_ok": (not mode) or (mode in t["modes"]),
        "axis_ok": (not axis) or (axis in t["axes"]),
        "domain_ok": (not domain) or (domain in (md.get("domains", []) if md else [])),
        "state_ok": (not state) or (state in t["states"]),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(load(), indent=2, ensure_ascii=False))

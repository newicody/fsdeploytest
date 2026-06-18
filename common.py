#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
common.py — socle commun des scripts USERSPACE (analyse, build, replication...).

NE PAS importer depuis init.py / session_launch.py / build_initramfs.py : ceux-la
tournent dans l'initramfs et restent AUTONOMES (sans dependance), par surete du
PID 1. common.py est pour le code userspace (chroot / systeme boote), ou importer
un module commun est sans risque.

Centralise ce qui etait duplique dans 6+ scripts :
  - sh()            : execution shell unifiee (remplace _sh/sh/run/capture)
  - Result          : UN objet resultat observable (remplace Outcome/SfsResult/
                      UkiResult/RepResult/MountState/Check...). Acces par
                      attributs ; un attribut absent leve un message clair.
  - helpers ZFS     : dataset_exists, zfs_get, mountpoint, where_mounted
  - is_true         : parsing booleen tolerant
  - load_config     : lecture configobj avec acces par attributs (ConfigView)

ASCII-only, stdlib (+ configobj pour load_config).
"""
import os
import subprocess


# --------------------------------------------------------------------------- #
# execution shell unifiee
# --------------------------------------------------------------------------- #
def sh(cmd, timeout=None, check=False):
    """Execute une commande. Retourne (rc, stdout, stderr) sans lever (sauf
    check=True). Remplace les multiples _sh/sh/capture des scripts."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} -> rc={p.returncode}: "
                               f"{p.stderr.strip()[:120]}")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} introuvable"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout ({timeout}s)"
    except OSError as e:
        return 1, "", str(e)


def sh_ok(cmd, timeout=None):
    """True si la commande reussit (rc==0)."""
    return sh(cmd, timeout=timeout)[0] == 0


# --------------------------------------------------------------------------- #
# objet resultat observable unique
# --------------------------------------------------------------------------- #
class Result:
    """Resultat d'une operation, interrogeable par ATTRIBUTS plutot que par
    exceptions. On LIT .ok / .failed / .reason / .value / .data[...] au lieu
    d'attraper une exception.

    bool(result) == result.ok  -> utilisable directement dans un if.
    Acces a une cle de data absente -> message clair (KeyError explicite).
    Remplace Outcome / SfsResult / UkiResult / RepResult / MountState / Check.
    """
    __slots__ = ("ok", "label", "reason", "value", "data")

    def __init__(self, ok, label="", reason="", value=None, **data):
        self.ok = bool(ok)
        self.label = label
        self.reason = reason
        self.value = value
        self.data = data            # champs libres (size_mb, where, mode...)

    @classmethod
    def success(cls, label="", value=None, **data):
        return cls(True, label, "", value, **data)

    @classmethod
    def failure(cls, label="", reason="", **data):
        return cls(False, label, reason, None, **data)

    @property
    def failed(self):
        return not self.ok

    def __bool__(self):
        return self.ok

    def __getitem__(self, key):
        """Acces aux champs data avec message clair si absent."""
        try:
            return self.data[key]
        except KeyError:
            raise KeyError(
                f"champ '{key}' absent du Result '{self.label}' ; "
                f"champs presents : {sorted(self.data)}") from None

    def get(self, key, default=None):
        return self.data.get(key, default)

    def then(self, fn):
        """Chaine : execute fn() seulement si ok ; propage l'echec sinon.
        fn renvoie un Result (ou une valeur -> success). Capture la frontiere."""
        if not self.ok:
            return self
        try:
            r = fn()
            return r if isinstance(r, Result) else Result.success(self.label, r)
        except Exception as e:
            return Result.failure(self.label, f"{type(e).__name__}: {e}")

    def __repr__(self):
        state = "ok" if self.ok else f"FAIL: {self.reason}"
        extra = f" {self.data}" if self.data else ""
        return f"<Result {self.label!r} {state}{extra}>"


def step(label, fn):
    """Enveloppe un appel en Result sans laisser fuiter d'exception."""
    try:
        r = fn()
        return r if isinstance(r, Result) else Result.success(label, r)
    except Exception as e:
        return Result.failure(label, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# helpers ZFS partages
# --------------------------------------------------------------------------- #
def dataset_exists(dataset):
    return sh_ok(["zfs", "list", "-H", "-o", "name", dataset])


def zfs_get(dataset, prop):
    """Valeur d'une propriete ZFS ('' si erreur)."""
    rc, out, _ = sh(["zfs", "get", "-H", "-o", "value", prop, dataset])
    return out if rc == 0 else ""


def mountpoint(dataset):
    """Propriete mountpoint ('legacy', '/chemin', ou '')."""
    return zfs_get(dataset, "mountpoint")


def proc_mounts():
    """Genere (source, target, fstype) depuis /proc/mounts."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    yield p[0], p[1], p[2]
    except OSError:
        return


def where_mounted(dataset):
    """Ou le dataset ZFS est REELLEMENT monte (verite /proc/mounts) ; '' sinon."""
    for source, target, fstype in proc_mounts():
        if fstype == "zfs" and source == dataset:
            return target
    return ""


def is_mounted(dataset):
    """Le dataset est-il reellement monte (where + ismount) ?"""
    w = where_mounted(dataset)
    return bool(w) and os.path.ismount(w)


# --------------------------------------------------------------------------- #
# parsing / config
# --------------------------------------------------------------------------- #
def is_true(v):
    """Parsing booleen tolerant (true/1/yes/oui)."""
    return str(v).strip().lower() in ("true", "1", "yes", "oui")


class ConfigView:
    """Acces par attribut a une config configobj (cfg.section.cle), avec
    affichage. Cle/section absente -> None (pas d'exception). Sous-sections =
    ConfigView. Centralise la version qui etait dans first_boot.py."""
    __slots__ = ("_d", "_name")

    def __init__(self, d, name="config"):
        object.__setattr__(self, "_d", d)
        object.__setattr__(self, "_name", name)

    def __getattr__(self, key):
        if key in ("_d", "_name"):
            return object.__getattribute__(self, key)
        val = self._d.get(key)
        if isinstance(val, dict):
            return ConfigView(val, f"{self._name}.{key}")
        return val

    def __getitem__(self, key):
        return self.__getattr__(key)

    def get(self, key, default=None):
        val = self._d.get(key, default)
        return ConfigView(val, f"{self._name}.{key}") if isinstance(val, dict) else val

    def __contains__(self, key):
        return key in self._d

    def keys(self):
        return self._d.keys()

    def items(self):
        for k, v in self._d.items():
            yield k, (ConfigView(v, f"{self._name}.{k}") if isinstance(v, dict) else v)

    def render(self, indent=0):
        pad = "  " * indent
        for k, v in self._d.items():
            if isinstance(v, dict):
                yield f"{pad}[{k}]"
                yield from ConfigView(v, k).render(indent + 1)
            else:
                shown = ", ".join(v) if isinstance(v, list) else v
                yield f"{pad}{k} = {shown}"

    def show(self, title=None, log=print):
        log(f"\n--- {title or self._name} ---")
        for line in self.render():
            log(f"  {line}")
        log("---")

    def raw(self):
        """Le dict configobj sous-jacent (pour les fonctions qui veulent le brut)."""
        return self._d


def load_config(path="infra.conf", as_view=True):
    """Charge un INI configobj. Retourne ConfigView (defaut) ou le dict brut.
    Leve un message clair si configobj absent ou fichier introuvable."""
    try:
        from configobj import ConfigObj
    except ImportError:
        raise RuntimeError("configobj requis : emerge dev-python/configobj")
    if not os.path.exists(path):
        raise FileNotFoundError(f"config introuvable : {path}")
    cfg = ConfigObj(path)
    return ConfigView(cfg, os.path.basename(path)) if as_view else cfg


if __name__ == "__main__":
    # auto-test rapide
    r = Result.success("demo", value=42, size_mb=10)
    print(repr(r), "| ok:", r.ok, "| value:", r.value, "| size_mb:", r["size_mb"])
    f = Result.failure("demo2", "boom")
    print(repr(f), "| bool:", bool(f), "| failed:", f.failed)
    print("is_true('oui'):", is_true("oui"), "| is_true('non'):", is_true("non"))
    try:
        r["absent"]
    except KeyError as e:
        print("acces champ absent ->", str(e)[:60])

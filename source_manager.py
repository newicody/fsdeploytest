#!/usr/bin/env python3
"""source_manager.py -- cycle de vie des SOURCES NOYAU sur fast_pool/usr-src.

Le maillon manquant de l'architecture : RECUPERER une source (tarball kernel.org),
la poser sur le dataset, SELECTIONNER /usr/src/linux, LISTER, TRACER. C'est lui qui
possede le "repoint /usr/src/linux" que kernel_build (garde-fou KVER_EXPECT) attend,
et que first_boot appelle au bootstrap.

  list           sources presentes + laquelle est active (/usr/src/linux)
  fetch [<ver>]  telecharge+extrait linux-<ver> (kernel.org) sur le dataset
  select <ver>   pointe /usr/src/linux -> linux-<ver> (symlink atomique)
  remove <ver>   supprime un arbre source

Les arbres vivent dans le dossier du dataset fast_pool/usr-src (= dirname de
[kernel] src, soit /usr/src) : /usr/src/linux-<ver> ; /usr/src/linux est le lien
vers l'actif. Trace via kernel_registry (manager). Reutilise [kernel] d'infra.conf.
"""
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

DEFAULT_MIRROR = "https://cdn.kernel.org/pub/linux/kernel"


def _kernel_cfg(infra_path=None):
    """Lit [kernel] : src (/usr/src/linux), version (defaut), mirror (kernel.org)."""
    src, version, mirror = "/usr/src/linux", "", DEFAULT_MIRROR
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(infra_path or os.environ.get("INFRA_CONF", "infra.conf"))
        k = cfg.get("kernel", {}) or {}
        src = k.get("src", src)
        version = k.get("version", version)
        mirror = (k.get("mirror", mirror) or DEFAULT_MIRROR).rstrip("/")
    except Exception:
        pass
    return {"src": src, "version": version, "mirror": mirror}


def _container(src):
    """Dossier des arbres source = dirname du lien src (le dataset usr-src)."""
    return os.path.dirname(src.rstrip("/")) or "/usr/src"


def _tree(container, ver):
    return os.path.join(container, f"linux-{ver}")


def _major_dir(ver):
    """Repertoire kernel.org : 6.12.3 -> v6.x ; 5.15.1 -> v5.x."""
    return f"v{ver.split('.')[0]}.x"


def tarball_url(mirror, ver):
    return f"{mirror}/{_major_dir(ver)}/linux-{ver}.tar.xz"


def list_sources(container, src):
    """(liste des versions presentes, version active ou None)."""
    trees = []
    if os.path.isdir(container):
        trees = sorted(d[len("linux-"):] for d in os.listdir(container)
                       if d.startswith("linux-")
                       and os.path.isdir(os.path.join(container, d)))
    active = None
    if os.path.islink(src):
        tgt = os.path.basename(os.path.realpath(src))
        if tgt.startswith("linux-"):
            active = tgt[len("linux-"):]
    return trees, active


def fetch(ver, container, mirror, sha256=None, log=print):
    """Telecharge linux-<ver>.tar.xz (kernel.org) et l'extrait sur le dataset.
    Idempotent (si l'arbre est la, on ne refait rien). sha256 verifie si fourni."""
    dst = _tree(container, ver)
    if os.path.isdir(dst):
        log(f"[source] linux-{ver} deja presente ({dst})")
        return dst
    os.makedirs(container, exist_ok=True)
    url = tarball_url(mirror, ver)
    log(f"[source] telechargement {url}")
    tmp = tempfile.NamedTemporaryFile(dir=container, suffix=".tar.xz", delete=False)
    tmp.close()
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp.name, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
        if sha256 and h.hexdigest().lower() != sha256.lower():
            raise RuntimeError(f"sha256 incorrect (attendu {sha256}, "
                               f"obtenu {h.hexdigest()})")
        log(f"[source] extraction -> {dst}")
        with tarfile.open(tmp.name) as tf:
            tf.extractall(container, filter="data")   # cree container/linux-<ver>
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if not os.path.isdir(dst):
        raise RuntimeError(f"extraction : {dst} absent apres tar")
    log(f"[source] linux-{ver} prete")
    _track("source-fetch", ver, log)
    return dst


def select(ver, container, src, log=print):
    """Pointe /usr/src/linux -> linux-<ver> (symlink RELATIF, remplacement atomique)."""
    dst = _tree(container, ver)
    if not os.path.isdir(dst):
        raise RuntimeError(f"linux-{ver} absente ({dst}) ; 'fetch' d'abord")
    tmp = src + ".new"
    try:
        os.remove(tmp)
    except OSError:
        pass
    os.symlink(f"linux-{ver}", tmp)        # relatif dans le dossier du dataset
    os.replace(tmp, src)
    log(f"[source] {src} -> linux-{ver}")
    _track("source-select", ver, log)
    return True


def remove(ver, container, src, log=print):
    """Supprime un arbre source (refuse si c'est l'actif)."""
    _, active = list_sources(container, src)
    if ver == active:
        raise RuntimeError(f"linux-{ver} est ACTIVE ; 'select' une autre d'abord")
    dst = _tree(container, ver)
    if not os.path.isdir(dst):
        raise RuntimeError(f"linux-{ver} absente")
    shutil.rmtree(dst)
    log(f"[source] linux-{ver} supprimee")
    return True


def ensure(version, container, mirror, src, log=print):
    """Bootstrap (first_boot) : si aucune source ACTIVE, fetch + select 'version'."""
    _, active = list_sources(container, src)
    if active:
        log(f"[source] active deja en place : linux-{active}")
        return active
    if not version:
        log("[source] aucune source active et [kernel] version vide -> rien a poser")
        return None
    fetch(version, container, mirror, log=log)
    select(version, container, src, log=log)
    return version


def _track(kind, ver, log):
    try:
        import kernel_registry
        kernel_registry.KernelRegistry().log_event(kind, None, f"source linux-{ver}")
    except Exception as e:
        log(f"[source] trace manager indisponible ({e})")


def main():
    import argparse
    cfg = _kernel_cfg()
    container = _container(cfg["src"])
    ap = argparse.ArgumentParser(description="Gestion des sources noyau (usr-src).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    f = sub.add_parser("fetch")
    f.add_argument("version", nargs="?", help="defaut: [kernel] version")
    f.add_argument("--sha256", default=None)
    f.add_argument("--select", action="store_true", help="selectionner apres fetch")
    s = sub.add_parser("select")
    s.add_argument("version")
    r = sub.add_parser("remove")
    r.add_argument("version")
    a = ap.parse_args()

    if a.cmd == "list":
        trees, active = list_sources(container, cfg["src"])
        if not trees:
            print(f"(aucune source dans {container})")
        for v in trees:
            print(("* " if v == active else "  ") + f"linux-{v}")
        return 0
    if a.cmd == "fetch":
        ver = a.version or cfg["version"]
        if not ver:
            sys.exit("version requise (argument ou [kernel] version)")
        fetch(ver, container, cfg["mirror"], sha256=a.sha256)
        if a.select:
            select(ver, container, cfg["src"])
        return 0
    if a.cmd == "select":
        select(a.version, container, cfg["src"])
        return 0
    if a.cmd == "remove":
        remove(a.version, container, cfg["src"])
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

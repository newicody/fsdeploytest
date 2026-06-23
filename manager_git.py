#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""manager_git.py -- synchro git de boot_pool/manager (audit trail git-friendly).

C'est la "synchro git, module separe" referencee par kernel_registry : elle
commit + push (best-effort) le contenu TEXTE versionnable du manager
(manifest.json, history.jsonl, kernels/**/.config, configs/**) vers un remote
configure dans infra.conf [manager] git_remote. Permet que TOUTES les operations
journalisees (compile, promote, first-boot, operate, inference...) remontent
dans git.

Principes :
  - Best-effort ABSOLU : aucune exception ne remonte a l'appelant. Une operation
    n'est jamais bloquee ni mise en echec par la synchro git (offline, remote
    non configure, git absent -> on consigne et on continue).
  - Le commit est LOCAL et durable (boot_pool = mirror) meme sans remote : l'etat
    s'accumule et le prochain push rattrape tout.
  - Le token n'est JAMAIS ecrit dans .git/config : il est injecte uniquement dans
    l'URL passee en ligne a 'git push' (ephemere).

Usage : manager_git.sync("promote 7.0.12", root="/boot_pool/manager")
"""
import os
import subprocess
from pathlib import Path
from shutil import which


def _infra(path=None):
    """Charge infra.conf (env INFRA_CONF, sinon chemins usuels). None si absent."""
    for c in (path, os.environ.get("INFRA_CONF"), "/etc/infra.conf",
              "/infra.conf", "/sbin/infra.conf"):
        if c and os.path.exists(c):
            try:
                from configobj import ConfigObj
                return ConfigObj(c)
            except Exception:
                return None
    return None


def manager_root():
    """Racine du manager : MANAGER_ROOT (env) > [manager] root > defaut."""
    if os.environ.get("MANAGER_ROOT"):
        return Path(os.environ["MANAGER_ROOT"])
    cfg = _infra()
    mr = (cfg.get("manager", {}) or {}).get("root") if cfg else None
    return Path(mr or "/boot_pool/manager")


def _git_config():
    """(remote, branch) depuis [manager] ; remote vide = push desactive."""
    cfg = _infra()
    m = (cfg.get("manager", {}) or {}) if cfg else {}
    return (m.get("git_remote") or "").strip(), (m.get("git_branch") or "main").strip()


def _auth_url(remote):
    """Injecte GITHUB_TOKEN dans une URL https github (auth push), ephemere."""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and remote.startswith("https://github.com/"):
        return remote.replace("https://", f"https://x-access-token:{tok}@", 1)
    return remote


def _git(root, *args, timeout=20):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=timeout)


def ensure_repo(root):
    """Initialise un depot git dans `root` s'il n'en est pas un. True si pret."""
    root = Path(root)
    if (root / ".git").is_dir():
        return True
    try:
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "appliance@localhost")
        _git(root, "config", "user.name", "appliance")
        # ne pas versionner les gros artefacts binaires (references par chemin)
        gi = root / ".gitignore"
        if not gi.exists():
            gi.write_text("*.sfs\n*.img\ninitramfs*\nvmlinuz*\n*.bak\n")
        return (root / ".git").is_dir()
    except Exception:
        return False


def sync(message, root=None, push=True, log=print):
    """add -A + commit + (push best-effort). Retourne True si tout a abouti
    (ou rien a committer), False si une etape a echoue. JAMAIS d'exception."""
    try:
        if not which("git"):
            return False
        root = Path(root) if root else manager_root()
        if not root.is_dir():
            return False
        if not ensure_repo(root):
            return False
        _git(root, "add", "-A")
        # rien a committer -> succes silencieux
        if _git(root, "diff", "--cached", "--quiet").returncode == 0:
            return True
        msg = (message or "update")[:200]
        if _git(root, "commit", "-q", "-m", msg).returncode != 0:
            return False
        if not push:
            return True                 # commit local durable (mirror) ; push plus tard
        remote, branch = _git_config()
        if not remote:
            return True                 # pas de remote configure -> local seul
        url = _auth_url(remote)          # token injecte uniquement ici (ephemere)
        r = _git(root, "push", url, f"HEAD:{branch}", timeout=30)
        if r.returncode != 0:
            log(f"[manager-git] push differe ({(r.stderr or '').strip()[:90]})")
            return False
        return True
    except subprocess.TimeoutExpired:
        try:
            log("[manager-git] git timeout -> differe (commits locaux conserves)")
        except Exception:
            pass
        return False
    except Exception as e:
        try:
            log(f"[manager-git] sync best-effort echoue ({e})")
        except Exception:
            pass
        return False


def main():
    import argparse
    ap = argparse.ArgumentParser(description="synchro git du manager")
    ap.add_argument("message", nargs="?", default="manual sync")
    ap.add_argument("--root", default=None)
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    ok = sync(a.message, root=a.root, push=not a.no_push)
    print("sync OK" if ok else "sync incomplet (voir messages)")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

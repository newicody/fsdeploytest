#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
kernel_registry.py — gestionnaire de versions de noyau (index + historique).

UN SEUL dataset pour le logiciel (boot_pool/manager, durable car mirror), avec
une arborescence simple sur fichiers (PAS un dataset ZFS par version) :

  boot_pool/manager/
    manifest.json            index des versions (statut, refs artefacts)
    kernels/<kver>/.config   la config compilee, archivee (texte)
    configs/<nom>/.config    configs ETUDIEES (pas forcement compilees)
    configs/<nom>/notes.md
    history.jsonl            journal append-only : inferences + compilations

Les gros artefacts (modules.sfs, initramfs, bzImage) restent REFERENCES par
chemin (ils vivent sur l'ESP / fast_pool), jamais copies dans l'arbre.

Mise a jour automatique : kernel_build.py enregistre (candidate) + journalise ;
boot_confirm.py promeut (current, l'ancienne -> fallback). Indexation SEULE :
aucun zfs destroy / rm. Le menage reste manuel, eclaire par audit().

Perimetre git : manifest.json, kernels/*/.config, configs/*, history.jsonl sont
du TEXTE versionnable (cf. la synchro git, module separe). Les .sfs/initramfs
n'y vont jamais.

Stdlib uniquement. Magic methods avec messages ; yield sur iteration/historique.
"""
import json
import os
import shutil
import time
from pathlib import Path

# statuts d'une version dans le cycle de boot
ST_CANDIDATE = "candidate"   # construite, pas encore validee par un boot
ST_CURRENT   = "current"     # bootee et promue (BootOrder[0])
ST_FALLBACK  = "fallback"    # ancienne current, gardee comme filet
ST_STALE     = "stale"       # plus referencee, candidate au menage manuel
VALID_ST = (ST_CANDIDATE, ST_CURRENT, ST_FALLBACK, ST_STALE)

# types d'evenements du journal
EV_COMPILE   = "compile"
EV_PROMOTE   = "promote"
EV_INFERENCE = "inference"
EV_STUDY     = "study"
EV_NOTE      = "note"
EV_FIRSTBOOT = "first-boot"  # orchestration first_boot (empreinte/build/finalisation)


def _infra_manager_root():
    """[manager] root de l'infra.conf PHYSIQUE (repli quand MANAGER_ROOT n'est
    pas dans l'env -- ex module appele hors operate/first_boot/session_launch).
    None si introuvable. Garde kernel_registry auto-suffisant sur le contrat."""
    for c in (os.environ.get("INFRA_CONF"), "/etc/infra.conf"):
        if c and os.path.isfile(c):
            try:
                from configobj import ConfigObj
                return (ConfigObj(c).get("manager", {}) or {}).get("root") or None
            except Exception:
                return None
    return None


def _manager_commit(message, root):
    """Commit local de l'audit trail (manager_git), best-effort. Le push est
    fait aux frontieres d'operation. Jamais bloquant pour le journal."""
    try:
        import manager_git
        manager_git.sync(message, root=str(root), push=False)
    except Exception:
        pass


class KernelRegistry:
    """Index des versions de noyau sur un arbre de fichiers unique."""

    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("MANAGER_ROOT")
                         or _infra_manager_root() or "/boot_pool/manager")
        self.manifest_path = self.root / "manifest.json"
        self.kernels_dir = self.root / "kernels"
        self.configs_dir = self.root / "configs"
        self.history_path = self.root / "history.jsonl"
        self._data = self._load()

    # --- manifeste ---------------------------------------------------------
    def _load(self):
        try:
            return json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            # manifest absent OU corrompu : tenter le backup avant d'abandonner
            try:
                data = json.loads((self.root / "manifest.json.bak").read_text())
                return data
            except (OSError, json.JSONDecodeError):
                return {"versions": {}}

    def _save(self):
        """Sauvegarde ATOMIQUE + backup, avec garde anti-perte d'historique.
        - refuse d'ecraser un manifest non-vide par un _data vide (corruption
          en memoire) -> protege contre l'auto-suppression de l'index.
        - ecrit dans un .tmp puis os.replace (atomique : jamais de fichier a
          moitie ecrit, meme si interrompu).
        - garde l'ancien manifest en .bak avant de remplacer."""
        self.root.mkdir(parents=True, exist_ok=True)
        # garde : ne pas remplacer un index peuple par un index vide
        if not self._data.get("versions"):
            if self.manifest_path.exists():
                try:
                    old = json.loads(self.manifest_path.read_text())
                    if old.get("versions"):
                        raise RuntimeError(
                            "refus de sauver un manifeste VIDE par-dessus un "
                            "manifeste peuple (protection anti-perte). "
                            "Verifie l'etat en memoire.")
                except json.JSONDecodeError:
                    pass
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        # backup de l'ancien avant de remplacer
        if self.manifest_path.exists():
            try:
                shutil.copy2(self.manifest_path, self.root / "manifest.json.bak")
            except OSError:
                pass
        os.replace(tmp, self.manifest_path)     # atomique
        return str(self.manifest_path)

    # --- acces avec messages ----------------------------------------------
    def __getitem__(self, kver):
        try:
            return self._data["versions"][kver]
        except KeyError:
            raise KeyError(
                f"version '{kver}' absente du manifeste ; connues : "
                f"{sorted(self._data['versions'])}") from None

    def __contains__(self, kver):
        return kver in self._data["versions"]

    def __iter__(self):
        for kver in sorted(self._data["versions"]):
            yield kver, self._data["versions"][kver]

    def versions(self):
        return sorted(self._data["versions"])

    # --- journal append-only (yield a la lecture) -------------------------
    def log_event(self, kind, kver=None, detail=""):
        self.root.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "kind": kind, "kver": kver,
               "detail": detail}
        with open(self.history_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # COMMIT git local (rapide, durable sur le mirror) : chaque operation
        # journalisee est tracee dans git. Le PUSH (remontee vers le remote) est
        # fait aux frontieres d'operation (operate/boot_confirm/first_boot) pour
        # ne pas mettre de latence reseau sur ce chemin. Best-effort.
        _manager_commit(f"{kind} {kver or ''}".strip() or kind, self.root)
        return rec

    def history(self, kind=None, kver=None):
        """Genere les evenements du journal (filtrables). Du plus ancien."""
        try:
            lines = self.history_path.read_text().splitlines()
        except OSError:
            return
        for ln in lines:
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if kind and rec.get("kind") != kind:
                continue
            if kver and rec.get("kver") != kver:
                continue
            yield rec

    # --- enregistrement d'une version compilee (kernel_build.py) ----------
    def register(self, kver, config=None, modules_sfs=None, bzimage=None,
                 initramfs=None, efi_entry=None, efi_loader=None,
                 status=ST_CANDIDATE):
        """Indexe une version. Archive le .config dans l'arbre (texte). Les
        gros artefacts sont references par chemin, pas copies."""
        kd = self.kernels_dir / kver
        kd.mkdir(parents=True, exist_ok=True)
        archived_cfg = None
        if config and os.path.exists(config):
            archived_cfg = str(kd / ".config")
            try:
                shutil.copy2(config, archived_cfg)
            except OSError:
                archived_cfg = None
        existing = self._data["versions"].get(kver, {})
        # VERSIONNING : on ne perd pas l'etat precedent. A chaque re-register,
        # on archive l'ancienne entree dans 'revisions' (incremental) et on
        # incremente 'rev'. L'historique de la version est ainsi conserve.
        revisions = existing.get("revisions", [])
        if existing:
            snapshot = {k: v for k, v in existing.items() if k != "revisions"}
            revisions = revisions + [snapshot]
        rev = existing.get("rev", 0) + 1 if existing else 1
        entry = {
            "kver": kver,
            "rev": rev,
            "config": archived_cfg or existing.get("config"),
            "modules_sfs": modules_sfs,
            "bzimage": bzimage,
            "initramfs": initramfs,
            "efi_entry": efi_entry,
            "efi_loader": efi_loader,
            "status": existing.get("status", status),
            "registered": existing.get("registered", int(time.time())),
            "updated": int(time.time()),
            "revisions": revisions,            # historique des etats precedents
        }
        self._data["versions"][kver] = entry
        self._save()
        self.log_event(EV_COMPILE, kver,
                       f"register rev={rev} status={entry['status']}")
        return entry

    # --- configs etudiees (pas forcement compilees) -----------------------
    def study_config(self, name, config_path, notes=""):
        """Archive une config etudiee sous configs/<nom>/ (texte, git-friendly)."""
        cd = self.configs_dir / name
        cd.mkdir(parents=True, exist_ok=True)
        if config_path and os.path.exists(config_path):
            shutil.copy2(config_path, cd / ".config")
        if notes:
            (cd / "notes.md").write_text(notes)
        self.log_event(EV_STUDY, None, f"config etudiee: {name}")
        return str(cd)

    # --- statut (boot_confirm.py) -----------------------------------------
    def mark(self, kver, status):
        if status not in VALID_ST:
            raise ValueError(
                f"statut invalide '{status}' ; attendu {VALID_ST}")
        if kver not in self._data["versions"]:
            raise KeyError(
                f"version '{kver}' inconnue ; connues : {self.versions()}")
        self._data["versions"][kver]["status"] = status
        self._data["versions"][kver]["updated"] = int(time.time())
        self._save()
        return self._data["versions"][kver]

    def promote(self, kver):
        """kver -> current ; l'ancienne current -> fallback."""
        for k, v in self._data["versions"].items():
            if v.get("status") == ST_CURRENT and k != kver:
                v["status"] = ST_FALLBACK
                v["updated"] = int(time.time())
        self.mark(kver, ST_CURRENT)
        self.log_event(EV_PROMOTE, kver, "promu current")
        return self.current()

    def current(self):
        for k, v in self._data["versions"].items():
            if v.get("status") == ST_CURRENT:
                return k
        return None

    def fallbacks(self):
        return [k for k, v in self._data["versions"].items()
                if v.get("status") == ST_FALLBACK]

    # --- audit de coherence (avant menage manuel) -------------------------
    def audit(self):
        """Compare manifeste <-> realite (fichiers references). Ne supprime
        RIEN ; dit ce qui est protege vs sur a nettoyer manuellement."""
        report = {}
        protected = {self.current(), *self.fallbacks()}
        for kver, v in self._data["versions"].items():
            problems = []
            for key in ("modules_sfs", "initramfs", "bzimage", "efi_loader",
                        "config"):
                path = v.get(key)
                if path and not os.path.exists(path):
                    problems.append(f"{key} introuvable ({path})")
            report[kver] = {
                "status": v.get("status"),
                "protected": kver in protected,
                "problems": problems,
                "safe_to_remove": (kver not in protected
                                   and v.get("status") == ST_STALE),
            }
        return report

    def print_audit(self):
        cur = self.current()
        print(f"=== Registre noyaux (root: {self.root}, current: "
              f"{cur or 'aucun'}) ===")
        rep = self.audit()
        for kver, r in sorted(rep.items()):
            flag = "[PROTEGE]" if r["protected"] else (
                "[SUPPRIMABLE]" if r["safe_to_remove"] else "")
            print(f"  {kver:18} {r['status']:10} {flag}")
            for p in r["problems"]:
                print(f"      ! {p}")
        stale = [k for k, r in rep.items() if r["safe_to_remove"]]
        if stale:
            print("\nSur a supprimer manuellement (eclean-kernel + l'arbre) :")
            for k in stale:
                print(f"  rm -rf {self.kernels_dir / k}   # + entree EFI / ESP")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="registre des versions de noyau")
    ap.add_argument("--root", default=None,
                    help="dataset du gestionnaire (defaut /boot_pool/manager)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("audit")
    ph = sub.add_parser("history"); ph.add_argument("--kind", default=None)
    pm = sub.add_parser("mark"); pm.add_argument("kver"); pm.add_argument("status")
    pp = sub.add_parser("promote"); pp.add_argument("kver")
    a = ap.parse_args()

    reg = KernelRegistry(root=a.root)
    if a.cmd == "list":
        for kver, v in reg:
            print(f"  {kver:18} {v.get('status'):10} "
                  f"sfs={'oui' if v.get('modules_sfs') else 'non'} "
                  f"efi={v.get('efi_entry') or '-'}")
    elif a.cmd == "audit":
        reg.print_audit()
    elif a.cmd == "history":
        for rec in reg.history(kind=a.kind):
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec["ts"]))
            print(f"  {t}  {rec['kind']:10} {rec.get('kver') or '-':18} "
                  f"{rec.get('detail','')}")
    elif a.cmd == "mark":
        print(reg.mark(a.kver, a.status))
    elif a.cmd == "promote":
        print("current ->", reg.promote(a.kver))


if __name__ == "__main__":
    main()

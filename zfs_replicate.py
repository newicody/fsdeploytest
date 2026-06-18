#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
zfs_replicate.py — replication incrementale de datasets (zfs send | zfs recv).

Cas d'usage principal : repliquer fast_pool/log (sur le stripe SANS redondance)
vers data_pool/log (raidz2, durable). fast_pool peut mourir entierement (1 NVMe
perdu = pool perdu) ; cette replication garde les logs au chaud sur data_pool.

Strategie : snapshots horodates + envoi INCREMENTAL.
  - 1er envoi : full (zfs send snap | zfs recv cible)
  - ensuite   : incremental (zfs send -i snap_precedent snap_courant | recv)
On retrouve le dernier snapshot COMMUN source/cible pour enchainer proprement.
Rotation : on garde les N derniers snapshots (cote source et cible).

Reutilisable pour d'autres datasets (overlays -> data_pool/archives, manager).
Retour observable (RepResult), pas d'exception qui fuit. ASCII-only, stdlib.
"""
import subprocess
import time

try:
    from common import sh as _common_sh
    def _sh(cmd):
        return _common_sh(cmd)
except ImportError:
    def _sh(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except OSError as e:
            return 1, "", str(e)


class RepResult:
    __slots__ = ("ok", "src", "dst", "mode", "snap", "reason")

    def __init__(self, ok, src, dst, mode="", snap="", reason=""):
        self.ok = bool(ok)
        self.src = src
        self.dst = dst
        self.mode = mode        # 'full' | 'incremental' | 'noop'
        self.snap = snap
        self.reason = reason

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return (f"<RepResult {self.src}->{self.dst} "
                f"{'OK' if self.ok else 'FAIL'} {self.mode} {self.snap} "
                f"{self.reason}>")


def _snapshots(dataset):
    """Liste triee (ancienne->recente) des noms de snapshots d'un dataset
    (juste la partie apres '@'). Vide si le dataset n'existe pas."""
    rc, out, _ = _sh(["zfs", "list", "-H", "-t", "snapshot", "-o", "name",
                      "-s", "creation", "-r", dataset])
    if rc != 0:
        return []
    snaps = []
    for line in out.splitlines():
        if line.startswith(dataset + "@"):
            snaps.append(line.split("@", 1)[1])
    return snaps


def _dataset_exists(dataset):
    rc, _, _ = _sh(["zfs", "list", "-H", "-o", "name", dataset])
    return rc == 0


def _make_snapshot(dataset, log):
    """Cree un snapshot horodate. Retourne le nom court (apres @) ou ''."""
    name = f"repl-{time.strftime('%Y%m%d-%H%M%S')}"
    rc, _, err = _sh(["zfs", "snapshot", f"{dataset}@{name}"])
    if rc != 0:
        log(f"  [!] snapshot {dataset}@{name} echoue : {err[:80]}")
        return ""
    log(f"  snapshot {dataset}@{name}")
    return name


def _last_common(src, dst):
    """Dernier snapshot present a la fois sur src et dst (base de l'incremental)."""
    if not _dataset_exists(dst):
        return ""
    src_snaps = set(_snapshots(src))
    for s in reversed(_snapshots(dst)):     # du plus recent au plus ancien
        if s in src_snaps:
            return s
    return ""


def _send_recv(src, dst, snap, base=None, log=print):
    """Execute zfs send [-i base] src@snap | zfs recv -F dst. Retourne (ok,err).
    On utilise un pipe shell (subprocess avec deux process chaines)."""
    if base:
        send = ["zfs", "send", "-i", f"{src}@{base}", f"{src}@{snap}"]
    else:
        send = ["zfs", "send", f"{src}@{snap}"]
    recv = ["zfs", "recv", "-F", dst]
    log(f"  {'incremental' if base else 'full'} : "
        + " ".join(send) + " | " + " ".join(recv))
    try:
        p1 = subprocess.Popen(send, stdout=subprocess.PIPE)
        p2 = subprocess.Popen(recv, stdin=p1.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p1.stdout.close()                   # permet a p1 de recevoir SIGPIPE
        _, err = p2.communicate()
        p1.wait()
        if p1.returncode != 0:
            return False, f"send rc={p1.returncode}"
        if p2.returncode != 0:
            return False, (err.decode(errors="replace")[:120] if err
                           else f"recv rc={p2.returncode}")
        return True, ""
    except OSError as e:
        return False, str(e)


def _rotate(dataset, keep, log):
    """Garde les `keep` derniers snapshots 'repl-*', detruit les plus vieux."""
    repl = [s for s in _snapshots(dataset) if s.startswith("repl-")]
    excess = repl[:-keep] if keep > 0 else []
    for s in excess:
        rc, _, err = _sh(["zfs", "destroy", f"{dataset}@{s}"])
        if rc == 0:
            log(f"  rotation : detruit {dataset}@{s}")
        else:
            log(f"  [!] rotation {dataset}@{s} : {err[:60]}")


def replicate(src, dst, keep=10, log=print):
    """Replique src -> dst de facon incrementale. Cree un snapshot courant,
    envoie full (1ere fois) ou incremental (depuis le dernier commun), puis
    applique la rotation des deux cotes. Retourne RepResult."""
    if not _dataset_exists(src):
        return RepResult(False, src, dst, reason="source absente")

    snap = _make_snapshot(src, log)
    if not snap:
        return RepResult(False, src, dst, reason="snapshot source echoue")

    base = _last_common(src, dst)
    if base == snap:                        # rien de neuf (improbable, meme nom)
        return RepResult(True, src, dst, "noop", snap, "deja a jour")

    ok, err = _send_recv(src, dst, snap, base=base or None, log=log)
    if not ok:
        # si l'incremental echoue (base divergente), tenter un full de secours
        if base:
            log(f"  [!] incremental echoue ({err}) -> tentative full")
            ok, err = _send_recv(src, dst, snap, base=None, log=log)
        if not ok:
            return RepResult(False, src, dst, reason=f"send/recv : {err}")

    mode = "incremental" if base else "full"
    _rotate(src, keep, log)
    if _dataset_exists(dst):
        _rotate(dst, keep, log)
    log(f"  replication OK ({mode}) {src} -> {dst}")
    return RepResult(True, src, dst, mode, snap)


def replicate_from_config(infra_conf="infra.conf", log=print):
    """Lit la section [replication] de infra.conf et replique chaque paire.
    Format :
        [replication]
            [[logs]]
            src = fast_pool/log
            dst = data_pool/log
            keep = 14
    Retourne la liste des RepResult."""
    try:
        from configobj import ConfigObj
    except ImportError:
        log("configobj absent -- replication par config indisponible")
        return []
    cfg = ConfigObj(infra_conf)
    sect = cfg.get("replication", {})
    results = []
    for name, decl in sect.items():
        if not isinstance(decl, dict):
            continue
        src = decl.get("src")
        dst = decl.get("dst")
        keep = int(decl.get("keep", 10))
        if not src or not dst:
            log(f"  [!] {name} : src/dst manquant")
            continue
        log(f">> replication '{name}' : {src} -> {dst} (keep={keep})")
        results.append(replicate(src, dst, keep=keep, log=log))
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="replication incrementale zfs send")
    ap.add_argument("--src", help="dataset source (ex: fast_pool/log)")
    ap.add_argument("--dst", help="dataset cible (ex: data_pool/log)")
    ap.add_argument("--keep", type=int, default=10, help="snapshots a conserver")
    ap.add_argument("--from-config", action="store_true",
                    help="lire [replication] de infra.conf")
    ap.add_argument("--infra", default="infra.conf")
    a = ap.parse_args()
    if a.from_config:
        res = replicate_from_config(a.infra)
    elif a.src and a.dst:
        res = [replicate(a.src, a.dst, keep=a.keep)]
    else:
        ap.error("fournir --src/--dst ou --from-config")
    ok = sum(1 for r in res if r.ok)
    print(f"\n{ok}/{len(res)} replication(s) OK")
    for r in res:
        print(" ", repr(r))
    raise SystemExit(0 if ok == len(res) else 1)

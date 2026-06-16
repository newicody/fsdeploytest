#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
first_boot.py — orchestrateur first-boot (depuis le chroot, SANS inference).

Prend un .config noyau + une declaration d'infra (infra.conf) et fait tout pour
configurer le boot, en suivant la meme ligne que le reste du projet :

  1. lit infra.conf, VERIFIE la conformite reel vs declare -> empreinte
     (ecart critique = STOP ; mineur = warning, on continue)
  2. compile le noyau + modules_install + emerge zfs-kmod (+ garde-fou zfs.ko)
  3. mksquashfs modules-<ver>.sfs + build_initramfs (zfs.ko frais embarque)
  4. stage ESP + entree EFI + BootNext ; enregistre dans le registre (candidate)
  5. rapport consolide (empreinte + build) ecrit dans boot_pool/manager
  6. demande l'AUTORISATION sur le board git avant de finaliser

Pas d'inference ici : en chroot le modele n'est pas actif (flag force a false).
Pas de kexec (changement de noyau a chaud) : reboot normal + BootNext (filet).
Reutilisation PAR IMPORT des modules rootfs (kernel_build, build_initramfs,
kernel_registry, github_board) ; init.py reste autonome (non importe).

Le stream (--stream) capture la SORTIE CONSOLE de l'orchestrateur (log texte)
vers YouTube : en chroot on suit la compilation a distance (pas de fbdev ici).

configobj pour la config. Le reste : stdlib + modules du projet.
"""
import argparse
import os
import subprocess
import sys
import time

try:
    from configobj import ConfigObj
except ImportError:
    ConfigObj = None


# --------------------------------------------------------------------------- #
# controle de flux par ATTRIBUTS observables (pas d'exceptions)
# --------------------------------------------------------------------------- #
class Outcome:
    """Resultat d'une etape, interrogeable par attributs plutot que par
    try/except. On LIT .ok / .failed / .reason / .value au lieu d'attraper une
    exception. bool(outcome) == outcome.ok, donc utilisable directement dans un
    if. Chainable : .then(fn) n'execute fn que si l'etape precedente a reussi."""
    __slots__ = ("_ok", "_reason", "_value", "_label")

    def __init__(self, ok, label="", reason="", value=None):
        self._ok = bool(ok)
        self._label = label
        self._reason = reason
        self._value = value

    @classmethod
    def success(cls, label="", value=None):
        return cls(True, label, "", value)

    @classmethod
    def failure(cls, label="", reason=""):
        return cls(False, label, reason, None)

    # --- attributs observables (lecture seule via property) ----------------
    @property
    def ok(self):
        return self._ok

    @property
    def failed(self):
        return not self._ok

    @property
    def reason(self):
        return self._reason

    @property
    def value(self):
        return self._value

    @property
    def label(self):
        return self._label

    def __bool__(self):
        return self._ok

    def then(self, fn, label=""):
        """Execute fn (qui renvoie un Outcome) seulement si on est ok. Sinon
        propage l'echec courant. Permet d'enchainer sans imbriquer des if."""
        if not self._ok:
            return self
        try:
            res = fn()
            return res if isinstance(res, Outcome) else Outcome.success(label, res)
        except Exception as e:                 # frontiere : on convertit en etat
            return Outcome.failure(label or self._label, f"{type(e).__name__}: {e}")

    def __repr__(self):
        state = "ok" if self._ok else f"FAIL: {self._reason}"
        return f"<Outcome {self._label!r} {state}>"


def step(label, fn):
    """Enveloppe un appel en Outcome sans laisser fuiter d'exception : on
    capture la frontiere une seule fois et on expose l'etat. fn renvoie soit un
    Outcome, soit une valeur (=> success), soit leve (=> failure)."""
    try:
        r = fn()
        if isinstance(r, Outcome):
            return r
        return Outcome.success(label, r)
    except Exception as e:
        return Outcome.failure(label, f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# utilitaires
# --------------------------------------------------------------------------- #
class Report:
    """Rapport consolide : empreinte de conformite + etapes de build."""
    def __init__(self):
        self.lines = []
        self.criticals = []     # ecarts critiques (bloquants)
        self.warnings = []      # ecarts mineurs

    def ok(self, msg):
        self.lines.append(f"[OK]    {msg}")

    def warn(self, msg):
        self.lines.append(f"[WARN]  {msg}")
        self.warnings.append(msg)

    def crit(self, msg):
        self.lines.append(f"[CRIT]  {msg}")
        self.criticals.append(msg)

    def info(self, msg):
        self.lines.append(f"[INFO]  {msg}")

    def text(self):
        head = (f"=== Empreinte / rapport first-boot "
                f"({time.strftime('%Y-%m-%d %H:%M')}) ===\n"
                f"  critiques: {len(self.criticals)}  "
                f"warnings: {len(self.warnings)}\n\n")
        return head + "\n".join(self.lines) + "\n"


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def is_true(v):
    return str(v).strip().lower() in ("true", "1", "yes", "oui")


# --------------------------------------------------------------------------- #
# vue de configuration : acces par ATTRIBUTS + affichage avant usage
# --------------------------------------------------------------------------- #
class ConfigView:
    """Enveloppe un dict/section configobj pour un acces par attribut
    (cfg.pools.fast_pool.type) au lieu de cfg['pools']['fast_pool']['type'],
    et un affichage lisible. Les sections imbriquees sont elles-memes des
    ConfigView. Acces a une cle absente -> None (pas d'exception), pour rester
    dans la logique 'controle, pas exception'."""
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
        return val                              # None si absent (pas d'erreur)

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
        """Genere (cle, valeur|ConfigView)."""
        for k, v in self._d.items():
            yield k, (ConfigView(v, f"{self._name}.{k}") if isinstance(v, dict) else v)

    def render(self, indent=0):
        """Genere les lignes d'affichage (recursif, via yield)."""
        pad = "  " * indent
        for k, v in self._d.items():
            if isinstance(v, dict):
                yield f"{pad}[{k}]"
                yield from ConfigView(v, k).render(indent + 1)
            else:
                shown = ", ".join(v) if isinstance(v, list) else v
                yield f"{pad}{k} = {shown}"

    def show(self, title=None):
        """Affiche la config avant usage (consommation du generateur render)."""
        print(f"\n--- {title or self._name} ---")
        for line in self.render():
            print(f"  {line}")
        print("---")


# --------------------------------------------------------------------------- #
# vue specifique inference (deux roles : copilot amont / local dispatch)
# --------------------------------------------------------------------------- #
class InferenceConfig:
    """Lit la section [inference] (copilot + local) et expose l'etat par
    attributs. En chroot, local est force off (in_chroot=True)."""
    __slots__ = ("copilot_on", "copilot_role", "local_on", "local_role",
                 "backend", "endpoint", "model", "router_model")

    def __init__(self, sect, in_chroot=True, force_local=None):
        cop = sect.get("copilot", {}) if sect else {}
        loc = sect.get("local", {}) if sect else {}
        self.copilot_on = is_true(cop.get("enabled", "false"))
        self.copilot_role = cop.get("role", "upstream-tagging")
        local_decl = is_true(loc.get("enabled", "false"))
        if force_local is not None:
            local_decl = force_local
        # regle de surete : jamais d'inference locale en chroot
        self.local_on = local_decl and not in_chroot
        self.local_role = loc.get("role", "dispatch")
        self.backend = loc.get("backend", "openvino")
        self.endpoint = loc.get("endpoint", "http://127.0.0.1:11434/v1")
        self.model = loc.get("model", "qwen3:30b")
        self.router_model = loc.get("router_model", "")

    @property
    def any_on(self):
        return self.copilot_on or self.local_on

    def summary(self):
        return (f"copilot={'on' if self.copilot_on else 'off'}({self.copilot_role}) "
                f"local={'on' if self.local_on else 'off'}({self.backend}/"
                f"{self.model})")


# --------------------------------------------------------------------------- #
# verification de conformite (reel vs declare) -> empreinte
# --------------------------------------------------------------------------- #
def check_pools(cfg, rep):
    sect = cfg.get("pools", {})
    sect_crit = is_true(sect.get("critical", "true"))
    rc, out, _ = sh(["zpool", "list", "-H", "-o", "name,health"])
    present = {}
    if rc == 0:
        for ln in out.splitlines():
            if ln.strip():
                name, health = (ln.split("\t") + ["?"])[:2]
                present[name] = health
    for name, decl in sect.items():
        if not isinstance(decl, dict):
            continue
        crit = is_true(decl.get("critical", sect_crit))
        if name not in present:
            (rep.crit if crit else rep.warn)(f"pool {name} DECLARE mais ABSENT")
        elif present[name].upper() not in ("ONLINE", "DEGRADED"):
            (rep.crit if crit else rep.warn)(
                f"pool {name} etat {present[name]}")
        else:
            note = " (DEGRADED)" if present[name].upper() == "DEGRADED" else ""
            rep.ok(f"pool {name} present ({present[name]}){note}, "
                   f"type declare {decl.get('type','?')}")


def check_datasets(cfg, rep):
    sect = cfg.get("datasets", {})
    for ds, decl in sect.items():
        if not isinstance(decl, dict):
            continue
        crit = is_true(decl.get("critical", "false"))
        rc, _, _ = sh(["zfs", "list", "-H", "-o", "name", ds])
        if rc != 0:
            (rep.crit if crit else rep.warn)(f"dataset {ds} DECLARE mais ABSENT")
            continue
        # proprietes attendues (toutes les cles sauf 'critical')
        diverg = []
        for prop, want in decl.items():
            if prop == "critical":
                continue
            g_rc, got, _ = sh(["zfs", "get", "-H", "-o", "value", prop, ds])
            if g_rc == 0 and got and got != str(want):
                diverg.append(f"{prop}={got} (attendu {want})")
        if diverg:
            (rep.warn)(f"dataset {ds} present mais divergent : "
                       + ", ".join(diverg))   # proprietes = warning, pas stop
        else:
            rep.ok(f"dataset {ds} conforme")


def check_efi(cfg, rep):
    sect = cfg.get("efi", {})
    crit = is_true(sect.get("critical", "true"))
    parts = sect.get("partitions", [])
    if isinstance(parts, str):
        parts = [parts]
    fstype = sect.get("fstype", "vfat")
    found = 0
    for p in parts:
        if not os.path.exists(p):
            rep.warn(f"ESP {p} declaree mais absente")
            continue
        rc, out, _ = sh(["blkid", "-o", "value", "-s", "TYPE", p])
        if fstype in out:
            rep.ok(f"ESP {p} presente ({out})")
            found += 1
        else:
            rep.warn(f"ESP {p} type {out or '?'} (attendu {fstype})")
    if found == 0:
        (rep.crit if crit else rep.warn)(
            "aucune ESP valide trouvee -> impossible de stager le noyau")


def check_firmware(cfg, rep, fw_root="/lib/firmware"):
    import glob
    sect = cfg.get("firmware", {})
    for group, decl in sect.items():
        if not isinstance(decl, dict):
            continue
        crit = is_true(decl.get("critical", "false"))
        pats = decl.get("patterns", [])
        if isinstance(pats, str):
            pats = [pats]
        for pat in pats:
            if glob.glob(os.path.join(fw_root, pat)):
                rep.ok(f"firmware {pat} present")
            else:
                (rep.crit if crit else rep.warn)(
                    f"firmware {pat} DECLARE mais ABSENT")


def verify_infra(cfg, rep):
    """Lance toutes les verifications. Remplit rep. Retourne True si aucun
    ecart critique (sinon le first-boot doit s'arreter)."""
    check_pools(cfg, rep)
    check_datasets(cfg, rep)
    check_efi(cfg, rep)
    check_firmware(cfg, rep)
    return not rep.criticals


# --------------------------------------------------------------------------- #
# build (reutilise les modules existants par import)
# --------------------------------------------------------------------------- #
def preflight(cfg, src, rep):
    """Verifie les prerequis de build qui ne sont PAS dans infra.conf mais
    bloquent en chroot : efivarfs monte, ESP montees, /usr/src/linux sain.
    PROPOSE les commandes de correction sans les executer (choix : non
    intrusif). Retourne la liste des commandes a lancer (vide si tout est bon)."""
    todo = []

    # 1. efivarfs (sinon efibootmgr -> exit 2 'EFI variables not supported')
    if not os.path.ismount("/sys/firmware/efi/efivars") and \
       not os.path.exists("/sys/firmware/efi/efivars/dummy"):
        # heuristique : repertoire vide ou absent => pas monte
        try:
            empty = not os.listdir("/sys/firmware/efi/efivars")
        except OSError:
            empty = True
        if empty:
            rep.warn("efivarfs non monte -> efibootmgr echouera")
            todo.append("mount -t efivarfs efivarfs /sys/firmware/efi/efivars")

    # 2. /usr/src/linux : boucle de symlink / cible cassee
    link = src
    try:
        real = os.path.realpath(link)
        if not os.path.isdir(real):
            rep.crit(f"{link} -> {real} : cible invalide (symlink casse ?)")
            todo.append(f"# corrige le lien : rm {link} ; "
                        f"ln -s /usr/src/linux-<version> {link}")
        elif not os.path.exists(os.path.join(real, "Makefile")):
            rep.warn(f"{link} ne contient pas de Makefile noyau")
    except OSError as e:
        rep.crit(f"{link} illisible ({e}) -- boucle de symlink ?")
        todo.append(f"# corrige le lien : rm {link} ; "
                    f"ln -s /usr/src/linux-<version> {link}")

    # 3. ESP du second disque (declarees mais non montees en chroot)
    efi = cfg.get("efi", {}) if hasattr(cfg, "get") else {}
    parts = efi.get("partitions", []) if efi else []
    if isinstance(parts, str):
        parts = [parts]
    # quelles ESP sont effectivement montees ?
    try:
        mounts = open("/proc/mounts").read()
    except OSError:
        mounts = ""
    for p in parts:
        if os.path.exists(p) and p not in mounts:
            rep.warn(f"ESP {p} presente mais NON montee (rsync 2e ESP impossible)")
            todo.append(f"mkdir -p /mnt/esp2 ; mount {p} /mnt/esp2  "
                        f"# pour synchroniser la 2e ESP")
    return todo


def run_build(config_path, rep, src="/usr/src/linux"):
    """Stage le .config fourni puis delegue a kernel_build.py (compile,
    zfs-kmod, sfs, initramfs, EFI, registre). Pas de reecriture : on appelle
    la chaine existante."""
    dst = os.path.join(src, ".config")
    if os.path.abspath(config_path) != os.path.abspath(dst):
        import shutil
        shutil.copy2(config_path, dst)
        rep.ok(f".config installe -> {dst}")
    # olddefconfig pour completer les symboles manquants sans interaction
    rc, _, err = sh(["make", "-C", src, "olddefconfig"], timeout=120)
    if rc != 0:
        rep.warn(f"olddefconfig: {err[:80]}")
    rep.info("delegation a kernel_build.py (compile + zfs-kmod + sfs + "
             "initramfs + EFI + registre)")
    # kernel_build.py est concu comme un script ; on l'execute (il lit l'env
    # SRC/ESP/DISK/PART/CMDLINE). On capture pour le rapport/stream.
    env = dict(os.environ, SRC=src)
    p = subprocess.run([sys.executable, "kernel_build.py"], env=env)
    return p.returncode == 0


# --------------------------------------------------------------------------- #
# autorisation git (reutilise le board)
# --------------------------------------------------------------------------- #
def request_git_authorization(rep, repo=None, kver=None):
    """Pousse le rapport/empreinte comme une idee 'first-boot' sur le board et
    demande l'autorisation. Sans repo/token configures, reste local (le rapport
    est deja ecrit) et on demande une confirmation sur la machine."""
    try:
        import brainstorm
        import github_board as gb
    except ImportError as e:
        rep.warn(f"board git indisponible ({e}) -> autorisation locale seulement")
        return _local_confirm()
    idea = brainstorm.from_note(
        f"First-boot {kver or ''}".strip(),
        rep.text())
    idea.act("kver", kver or "?")
    idea.act("criticals", len(rep.criticals))
    if repo and os.environ.get("GITHUB_TOKEN"):
        board = gb.Board(gb.GitHubTransport(repo))
        n = board.push(idea)
        rep.info(f"rapport pousse sur l'Issue #{n} ; passe le label en prod "
                 "pour autoriser, puis relance le watcher")
        return True            # l'autorisation se fera via le board (prod)
    rep.info("pas de repo/token git -> autorisation locale")
    return _local_confirm()


def _local_confirm():
    try:
        ans = input("\nAutoriser la finalisation du first-boot ? [y/N] ")
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "o", "yes", "oui")


def _push_failure(rep, repo, resume):
    """Remonte un ECHEC sur le board git (pas seulement le rapport final).
    Best-effort : si le board n'est pas configure, on consigne localement."""
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        return                              # pas de board -> deja dans le rapport local
    try:
        import brainstorm
        import github_board as gb
        idea = brainstorm.from_note(f"ECHEC first-boot : {resume}", rep.text())
        idea.set_status(brainstorm.S_DROP)   # echec -> colonne/label drop
        gb.Board(gb.GitHubTransport(repo)).push(idea)
        print(f"   (echec remonte sur le board : {repo})", flush=True)
    except Exception as e:
        print(f"   (remontee board impossible : {e})", flush=True)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="orchestrateur first-boot (chroot, sans inference)")
    ap.add_argument("--config", required=True, help="le .config noyau a utiliser")
    ap.add_argument("--infra", default="infra.conf",
                    help="declaration de l'infrastructure voulue")
    ap.add_argument("--src", default="/usr/src/linux")
    ap.add_argument("--repo", default=None, help="owner/name pour le board git")
    ap.add_argument("--no-inference", action="store_true",
                    help="(force en chroot) desactive tout appel d'inference")
    ap.add_argument("--enable-inference", action="store_true",
                    help="autorise l'inference (systeme boote uniquement)")
    ap.add_argument("--stream", action="store_true",
                    help="streame la console de l'orchestrateur vers YouTube")
    ap.add_argument("--dry-run", action="store_true",
                    help="verifie l'infra et s'arrete (pas de build)")
    a = ap.parse_args()

    if ConfigObj is None:
        sys.exit("configobj requis : emerge dev-python/configobj")
    if not os.path.exists(a.config):
        sys.exit(f".config introuvable : {a.config}")
    if not os.path.exists(a.infra):
        sys.exit(f"infra introuvable : {a.infra}")

    cfg_raw = ConfigObj(a.infra)
    cfg = ConfigView(cfg_raw, "infra")
    # AFFICHER la config avant de l'utiliser (par attributs/generateur)
    cfg.show(title=f"infra.conf (profil {cfg.profile or '?'})")

    # inference : deux roles. En chroot, local force off. --enable-inference
    # n'a d'effet QUE hors chroot (et reste soumis a la regle de surete).
    in_chroot = not a.enable_inference     # --enable-inference => systeme boote
    force_local = True if (a.enable_inference and not a.no_inference) else (
        False if a.no_inference else None)
    inf = InferenceConfig(cfg.get("inference", {}), in_chroot=in_chroot,
                          force_local=force_local)
    print(f">> first-boot profil={cfg.profile or '?'} inference[{inf.summary()}]",
          flush=True)

    stream = start_stream() if a.stream else None

    rep = Report()
    print(">> verification de l'infrastructure (reel vs declare)...", flush=True)
    coherent = verify_infra(cfg_raw, rep)
    print(rep.text(), flush=True)

    # ecrire l'empreinte dans le registre (boot_pool/manager), durable
    write_report(rep)

    if not coherent:
        print("!! ECART(S) CRITIQUE(S) -> first-boot STOPPE. "
              "Corrige l'infra (cf. rapport) puis relance.", flush=True)
        _push_failure(rep, a.repo, "infra non conforme")
        stop_stream(stream)
        sys.exit(2)

    # preflight : prerequis de build hors infra.conf (efivarfs, ESP, /usr/src)
    print(">> preflight (efivarfs, ESP, /usr/src/linux)...", flush=True)
    todo = preflight(cfg, a.src, rep)
    print(rep.text(), flush=True)
    if todo:
        print("!! prerequis manquants. Commandes a lancer AVANT de relancer "
              "first_boot :", flush=True)
        for cmd in todo:
            print(f"    {cmd}", flush=True)
        # un prerequis critique (rep.criticals nouveau) stoppe ; sinon on
        # laisse l'utilisateur decider (warnings) mais on s'arrete par securite
        # car efibootmgr/rsync echoueraient.
        print("\n   Lance ces commandes puis relance first_boot.py.", flush=True)
        _push_failure(rep, a.repo, "prerequis preflight manquants")
        stop_stream(stream)
        sys.exit(4)

    if a.dry_run:
        print(">> dry-run : infra conforme + preflight OK, arret avant build.",
              flush=True)
        stop_stream(stream)
        return

    print(">> build du noyau et du boot...", flush=True)
    built = run_build(a.config, rep, src=a.src)
    write_report(rep)
    if not built:
        print("!! build echoue (cf. sortie kernel_build). Rien arme.", flush=True)
        _push_failure(rep, a.repo, "build echoue")
        stop_stream(stream)
        sys.exit(3)

    kver = detect_kver(a.src)
    print(">> demande d'autorisation (git/local)...", flush=True)
    authorized = request_git_authorization(rep, repo=a.repo, kver=kver)
    write_report(rep)
    if authorized:
        print(f">> first-boot termine pour {kver}. BootNext arme (essai unique). "
              "Reboote pour tester ; boot_confirm promeut si sain.", flush=True)
    else:
        print(">> autorisation refusee/differee. Artefacts construits mais "
              "non finalises (tu peux autoriser via le board plus tard).", flush=True)
    stop_stream(stream)


# --- helpers stream / rapport / kver (locaux, stdlib) ---------------------- #
def detect_kver(src):
    rc, out, _ = sh(["make", "-C", src, "-s", "kernelrelease"], timeout=30)
    return out.strip() if rc == 0 and out.strip() else os.uname().release


def write_report(rep, path="/boot_pool/manager/first-boot-report.txt"):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(rep.text())
    except OSError:
        pass
    # journalise aussi dans le registre si dispo
    try:
        import kernel_registry
        kernel_registry.KernelRegistry().log_event(
            "study", None,
            f"first-boot empreinte: {len(rep.criticals)} crit, "
            f"{len(rep.warnings)} warn")
    except Exception:
        pass


def start_stream():
    """Streame la console (stdout courant) vers YouTube si ffmpeg + cle. Capture
    texte (chroot : pas de fbdev). Retourne le Popen ou None."""
    key = ""
    try:
        with open("/etc/yt.key") as f:
            key = f.read().strip()
    except OSError:
        pass
    ff = "/usr/local/bin/ffmpeg"
    if not key or not os.path.exists(ff):
        print("(stream demande mais cle/ffmpeg absent -> ignore)", flush=True)
        return None
    # rendu texte -> video : on encode un flux a partir d'une couleur + drawtext
    # simple n'est pas trivial sans fbdev ; ici on logge dans un fichier que
    # l'operateur peut suivre. Le vrai stream visuel arrive au boot (init.py).
    print("(stream chroot : suivi via /boot_pool/manager/first-boot-report.txt ; "
          "le stream video demarre au boot reel via init.py)", flush=True)
    return None


def stop_stream(proc):
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()

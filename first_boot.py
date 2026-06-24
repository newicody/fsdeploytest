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
    try:
        import zfs_mounts
    except ImportError:
        zfs_mounts = None
    # 0. les pools sont-ils importes ? (sinon les datasets paraissent absents)
    pools = set()
    for ds in cfg.get("datasets", {}):
        pools.add(ds.split("/", 1)[0])
    rc, imported, _ = sh(["zpool", "list", "-H", "-o", "name"])
    imported_set = set(imported.split()) if rc == 0 else set()
    for pool in sorted(pools):
        if pool in imported_set:
            rep.ok(f"pool {pool} importe")
        else:
            rep.crit(f"pool {pool} NON importe "
                     f"(zpool import {pool}) -> ses datasets seront introuvables")
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
            # 'critical'/'pool' ne sont pas des proprietes ZFS (metadonnees du
            # contrat) ; 'role' est un commentaire. On ne verifie que les vraies
            # proprietes ZFS du dataset.
            if prop in ("critical", "pool"):
                continue
            g_rc, got, _ = sh(["zfs", "get", "-H", "-o", "value", prop, ds])
            if g_rc == 0 and got and got != str(want):
                diverg.append(f"{prop}={got} (attendu {want})")
        if diverg:
            rep.warn(f"dataset {ds} present mais divergent : " + ", ".join(diverg))
        else:
            rep.ok(f"dataset {ds} conforme")
        # VERIFICATION DE MONTAGE (le bug : dossier cree mais pas monte).
        # Non bloquant ici (init.py monte au boot), mais on le SIGNALE.
        if zfs_mounts:
            st = zfs_mounts.inspect(ds)
            if not st.mounted:
                rep.warn(f"dataset {ds} existe mais N'EST PAS monte actuellement "
                         f"(verifier qu'init.py le montera, pas un dossier vide)")


def check_efi(cfg, rep):
    sect = cfg.get("efi", {})
    crit = is_true(sect.get("critical", "true"))
    fstype = sect.get("fstype", "vfat")
    try:
        import boot_layout
        esps = boot_layout.load_esps(os.environ.get("INFRA_CONF", "infra.conf"))
    except Exception as e:
        rep.warn(f"boot_layout indisponible ({e}) : verif ESP limitee")
        esps = []
    if not esps:
        (rep.crit if crit else rep.warn)("aucune ESP declaree dans [efi]")
        return
    found = 0
    for e in esps:
        dev = e.device()
        if not dev:
            rep.warn(f"ESP {e.name} : device introuvable "
                     f"(partuuid={e.partuuid or '-'}, part={e.partition or '-'})")
            continue
        rc, out, _ = sh(["blkid", "-o", "value", "-s", "TYPE", dev])
        if fstype not in out:
            rep.warn(f"ESP {e.name} ({dev}) type {out or '?'} (attendu {fstype})")
            continue
        # garde anti-/boot sur l'install_mount
        safe = e.safe_install_mount()
        if not safe:
            rep.warn(f"ESP {e.name} : {e.reason}")
            continue
        # figer le PARTUUID si absent dans l'ini (conseille pour la stabilite)
        if not e.partuuid:
            real = e.current_partuuid()
            if real:
                rep.warn(f"ESP {e.name} : PARTUUID absent de l'ini "
                         f"(ajoute 'partuuid = {real}' pour la stabilite)")
        rep.ok(f"ESP {e.name} : {dev} ({out}) -> install {safe} "
               f"{'[primary]' if e.primary else ''}")
        found += 1
    if found == 0:
        (rep.crit if crit else rep.warn)(
            "aucune ESP valide -> impossible de stager le noyau")


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
def _disk_free_gb(path):
    """Espace libre en Go sur le FS contenant path (0 si inaccessible)."""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except OSError:
        return 0.0


def preflight(cfg, src, rep):
    """Detecte TOUT ce qui peut casser AVANT d'agir : UEFI, arch, espace
    disque, outils, montages, /usr/src. Remplit rep (crit = stop). PROPOSE les
    commandes de correction sans les executer. Retourne la liste de commandes."""
    todo = []

    # --- 1. mode UEFI (sans /sys/firmware/efi, tout le schema EFI stub est mort)
    if not os.path.isdir("/sys/firmware/efi"):
        rep.crit("machine NON demarree en UEFI (/sys/firmware/efi absent) : "
                 "le boot EFI stub est impossible. Active UEFI dans le BIOS "
                 "(desactive le CSM/Legacy).")

    # --- 2. architecture (init.py est fige sur x86_64) ----------------------
    arch = os.uname().machine
    if arch != "x86_64":
        rep.crit(f"architecture {arch} : ce projet est fige sur x86_64 "
                 "(init.py : NR_finit_module, loader).")

    # --- 3. outils indispensables (manquant = echec garanti plus tard) ------
    tools = {"make": True, "gcc": True, "ld": True, "emerge": True,
             "mksquashfs": True, "efibootmgr": True, "zstd": True,
             "zpool": True, "zfs": True, "mount.zfs": True,
             "depmod": True, "modinfo": True}
    import shutil as _sh
    for tool, critical in tools.items():
        found = _sh.which(tool) or any(
            os.path.exists(os.path.join(d, tool))
            for d in ("/sbin", "/usr/sbin", "/usr/bin", "/bin"))
        if not found:
            (rep.crit if critical else rep.warn)(
                f"outil '{tool}' introuvable (installe le paquet correspondant)")

    # --- 4. espace disque (compiler + sfs + initramfs = plusieurs Go) -------
    checks = [(src, 8.0, "sources/compilation noyau"),
              ("/var/tmp", 6.0, "build portage (zfs-kmod)")]
    # ESP : besoin de qq centaines de Mo pour vmlinuz+initramfs
    esp = cfg.get("esp", {}) if hasattr(cfg, "get") else {}
    for path, need, what in checks:
        if os.path.exists(path):
            free = _disk_free_gb(path)
            if free < need:
                rep.crit(f"espace insuffisant sur {path} : {free:.1f} Go libres, "
                         f"~{need:.0f} Go requis ({what})")
            else:
                rep.ok(f"espace {path} : {free:.1f} Go libres ({what})")

    # --- 5. memoire (info ; 128 Go chez nous, mais on signale si maigre) -----
    try:
        mem = open("/proc/meminfo").read()
        total_kb = int(next(l.split()[1] for l in mem.splitlines()
                            if l.startswith("MemTotal")))
        gb = total_kb / (1024 ** 2)
        if gb < 4:
            rep.warn(f"memoire faible ({gb:.1f} Go) : reduis -j pour compiler")
        else:
            rep.ok(f"memoire : {gb:.0f} Go")
    except (OSError, StopIteration, ValueError):
        pass

    # --- 6. efivarfs monte (sinon efibootmgr exit 2) ------------------------
    if not _efivars_ok():
        rep.warn("efivarfs non monte -> efibootmgr echouera")
        todo.append("mount -t efivarfs efivarfs /sys/firmware/efi/efivars")

    # --- 7. /usr/src/linux sain (boucle de symlink ?) -----------------------
    try:
        real = os.path.realpath(src)
        if not os.path.isdir(real):
            rep.crit(f"{src} -> {real} : cible invalide (symlink casse ?)")
            todo.append(f"# rm {src} ; ln -s /usr/src/linux-<version> {src}")
        elif not os.path.exists(os.path.join(real, "Makefile")):
            rep.warn(f"{src} sans Makefile noyau")
    except OSError as e:
        rep.crit(f"{src} illisible ({e}) -- boucle de symlink ?")
        todo.append(f"# rm {src} ; ln -s /usr/src/linux-<version> {src}")

    # --- 8. ESP montees (via le CONTRAT [efi] = sous-sections esp1/esp2, pas
    #        une liste a plat 'partitions' qui n'existe pas) -------------------
    try:
        mounts = open("/proc/mounts").read()
    except OSError:
        mounts = ""
    try:
        import boot_layout
        for e in boot_layout.load_esps(os.environ.get("INFRA_CONF", "infra.conf")):
            dev = e.device()
            if dev and dev not in mounts:
                rep.warn(f"ESP {dev} presente mais NON montee")
                todo.append(f"mkdir -p {e.install_mount} ; "
                            f"mount {dev} {e.install_mount}")
    except Exception as ex:
        rep.warn(f"verif ESP via boot_layout impossible ({ex})")

    # --- 9. DROITS D'ECRITURE sur les cibles du build (resolues du contrat) --
    # first_boot ecrit dans : fast_pool/sfs (modules-<ver>.sfs + rootfs.sfs),
    # fast_pool/staging (rootfs en construction), usr-src (compilation), manager
    # (rapport/registre). Chaque cible doit etre MONTEE et INSCRIPTIBLE -- sinon
    # echec en plein build. On le detecte ICI, via les mountpoints d'infra.conf.
    _check_write_access(src, rep)

    return todo


def _ds_mountpoint(ds):
    """Mountpoint REEL d'un dataset (zfs get). '' si non monte/legacy/none."""
    try:
        p = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint", ds],
                           capture_output=True, text=True).stdout.strip()
        return p if p not in ("", "-", "none", "legacy") else ""
    except OSError:
        return ""


def _check_write_access(src, rep):
    """Verifie que first_boot peut ECRIRE sur tout ce dont il a besoin, AVANT
    d'agir. Cibles resolues depuis le contrat ([datasets] mountpoints via ZFS,
    [manager] root via MANAGER_ROOT). Non monte ou lecture seule = critique."""
    # (dataset, description). staging est la zone de construction reconstructible.
    for ds, what in (("fast_pool/sfs", "images sfs (modules-<ver>.sfs, rootfs.sfs)"),
                     ("fast_pool/staging", "staging de construction du rootfs")):
        mp = _ds_mountpoint(ds)
        if not mp:
            rep.crit(f"{ds} non monte -> {what} impossible "
                     f"(monte-le : zfs mount {ds})")
        elif not os.path.isdir(mp):
            rep.crit(f"{ds} monte sur {mp} mais repertoire absent ({what})")
        elif not os.access(mp, os.W_OK):
            rep.crit(f"PAS de droit d'ecriture sur {mp} ({what}) -- "
                     f"lance first_boot en root ou corrige les permissions")
        else:
            rep.ok(f"ecriture OK : {ds} -> {mp} ({what})")
    # usr-src (compilation + modules_install) et manager (rapport/registre)
    extra = [(os.path.dirname(os.path.realpath(src)) or src,
              "compilation noyau / modules_install"),
             (os.environ.get("MANAGER_ROOT", "/boot_pool/manager"),
              "rapport + registre (manager)")]
    for path, what in extra:
        probe = path if os.path.isdir(path) else (os.path.dirname(path) or "/")
        if not os.access(probe, os.W_OK):
            rep.crit(f"PAS de droit d'ecriture sur {probe} ({what}) -- "
                     f"lance first_boot en root ou corrige les permissions")
        else:
            rep.ok(f"ecriture OK : {probe} ({what})")


def _efivars_ok():
    p = "/sys/firmware/efi/efivars"
    if os.path.ismount(p):
        return True
    try:
        return bool(os.listdir(p))
    except OSError:
        return False


def run_build(config_path, rep, src="/usr/src/linux", infra_conf="infra.conf"):
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
    # SRC/ESP/DISK/PART/CMDLINE/INFRA_CONF). On capture pour le rapport/stream.
    # INFRA_CONF est CRUCIAL : sans lui, kernel_build ne trouve pas [uki] et ne
    # cree QUE l'entree classique (pas les profils safe/debug/i915).
    env = dict(os.environ, SRC=src,
               INFRA_CONF=os.path.abspath(infra_conf))
    p = subprocess.run([sys.executable, "kernel_build.py"], env=env)
    return p.returncode == 0


# --------------------------------------------------------------------------- #
# autorisation git (reutilise le board)
# --------------------------------------------------------------------------- #
def request_git_authorization(rep, repo=None, kver=None, bypass=False):
    """Pousse le rapport/empreinte comme une idee 'first-boot' sur le board et
    demande l'autorisation. Sans repo/token configures, reste local (le rapport
    est deja ecrit) et on demande une confirmation sur la machine.

    bypass=True (option --yes / --no-validation) : court-circuite TOUT (board git
    ET invite locale) et finalise directement. Aucun token ni board requis ;
    utile en execution non-interactive (sinon _local_confirm fait input() ->
    EOFError -> refus 'differe')."""
    if bypass:
        rep.info("validation court-circuitee (--yes) : finalisation autorisee "
                 "sans board git ni invite locale.")
        return True
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
    ap.add_argument("--rootfs-src", default=None,
                    help="racine Gentoo a figer en rootfs.sfs (si absent)")
    ap.add_argument("--force-live", action="store_true",
                    help="autorise rootfs.sfs depuis une racine non nettoyee "
                         "(sans marqueur clean_rootfs)")
    ap.add_argument("--appliance-ref", default=None,
                    help="repertoire des scripts appliance a jour "
                         "(session_launch.py, boot_confirm.py) a deployer dans le "
                         "rootfs avant de figer le sfs. Defaut : dossier de first_boot.")
    ap.add_argument("--no-force-sfs", dest="force_sfs", action="store_false",
                    help="ne PAS recreer rootfs.sfs s'il existe deja "
                         "(par defaut on le recree pour deployer les scripts a jour)")
    ap.set_defaults(force_sfs=True)
    ap.add_argument("--repo", default=None, help="owner/name (surcharge [git].repo)")
    ap.add_argument("--owner", default=None,
                    help="proprietaire du Project (surcharge [git].project_owner)")
    ap.add_argument("--number", default=None, type=int,
                    help="numero du Project (surcharge [git].project_number)")
    ap.add_argument("--no-inference", action="store_true",
                    help="(force en chroot) desactive tout appel d'inference")
    ap.add_argument("--enable-inference", action="store_true",
                    help="autorise l'inference (systeme boote uniquement)")
    ap.add_argument("--stream", action="store_true",
                    help="streame la console de l'orchestrateur vers YouTube")
    ap.add_argument("--yes", "-y", "--no-validation", dest="bypass_validation",
                    action="store_true",
                    help="court-circuite la validation (board git / invite "
                         "locale) et finalise directement le first-boot : "
                         "autorise sans rien demander (utile en non-interactif).")
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
    # manager : racine UNIQUE (registre + rapport + journal). infra.conf
    # [manager] root, sauf si MANAGER_ROOT deja impose par l'env (env > config).
    # On l'exporte AVANT tout usage du registre pour que kernel_registry,
    # write_report et la journalisation pointent au meme endroit.
    manager_root = (cfg_raw.get("manager", {}) or {}).get("root") \
        or "/boot_pool/manager"
    os.environ.setdefault("MANAGER_ROOT", manager_root)
    # config git : [git] dans infra.conf, surchargee par les options CLI
    gitc = cfg.get("git", {})
    repo = a.repo or (gitc.repo if gitc else None)
    git_mode = (gitc.mode if gitc else None) or "issues"
    proj_owner = a.owner or (gitc.project_owner if gitc else None)
    proj_number = a.number or (gitc.project_number if gitc else None)
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
        _push_failure(rep, repo, "infra non conforme")
        stop_stream(stream)
        sys.exit(2)

    # preflight EXHAUSTIF : UEFI, arch, espace, outils, montages, /usr/src
    print(">> preflight (UEFI, arch, espace disque, outils, montages)...",
          flush=True)
    crit_before = len(rep.criticals)
    todo = preflight(cfg, a.src, rep)
    print(rep.text(), flush=True)
    new_crit = len(rep.criticals) > crit_before
    if new_crit or todo:
        if new_crit:
            print("!! CONTEXTE INCOMPATIBLE (ecart critique) -> first-boot "
                  "STOPPE. Corrige les points [CRIT] ci-dessus.", flush=True)
        if todo:
            print("!! Commandes a lancer AVANT de relancer first_boot :",
                  flush=True)
            for cmd in todo:
                print(f"    {cmd}", flush=True)
        _push_failure(rep, repo, "preflight : contexte incompatible")
        stop_stream(stream)
        sys.exit(4)
    print(">> contexte compatible.", flush=True)

    if a.dry_run:
        print(">> dry-run : infra conforme + preflight OK, arret avant build.",
              flush=True)
        stop_stream(stream)
        return

    print(">> build du noyau et du boot...", flush=True)
    # rootfs.sfs : le creer s'il est absent (sinon init.py ne peut pas monter /)
    if a.rootfs_src:
        import sfs_build
        import inspect
        # GARDE SYNCHRO : si le sfs_build de la machine est une version anterieure
        # (sans ref_dir), on le DIT clairement au lieu d'un TypeError cryptique.
        params = inspect.signature(sfs_build.build_rootfs_sfs).parameters
        if "ref_dir" not in params:
            print("!! sfs_build.py est une ANCIENNE version (build_rootfs_sfs sans "
                  "ref_dir).", flush=True)
            print("   Mets a jour sfs_build.py EN MEME TEMPS que first_boot.py "
                  "(ils ont ete modifies ensemble).", flush=True)
            print("   first_boot.py et sfs_build.py doivent toujours etre "
                  "synchronises.", flush=True)
            sys.exit(4)
        # ref_dir : ou trouver session_launch.py/boot_confirm.py A JOUR a deployer
        # dans le rootfs avant de figer. Defaut = repertoire de first_boot.py.
        ref = getattr(a, "appliance_ref", None) or os.path.dirname(
            os.path.abspath(__file__))
        rs = sfs_build.build_rootfs_sfs(a.rootfs_src, "fast_pool/sfs",
                                        log=lambda m: print("   " + m, flush=True),
                                        force=getattr(a, "force_sfs", True),
                                        force_live=getattr(a, "force_live", False),
                                        ref_dir=ref)
        if not rs.ok:
            print(f"!! creation rootfs.sfs echouee : {rs.reason}", flush=True)
            _push_failure(rep, repo, f"rootfs.sfs : {rs.reason}")
            stop_stream(stream)
            sys.exit(3)
        rep.ok(f"rootfs.sfs : {rs.path} ({rs.size_mb} Mo)")
    else:
        rep.warn("--rootfs-src NON fourni : rootfs.sfs PAS regenere.")
        rep.warn("  => session_launch.py / boot_confirm.py NE SONT PAS mis a jour "
                 "dans le sfs : le boot utilisera l'ANCIENNE version figee.")
        rep.warn("  => Si tu viens de modifier ces scripts, relance AVEC "
                 "--rootfs-src <racine> pour qu'ils soient redeployes + refiges.")
        # detecter un piege frequent : scripts modifies recemment mais sfs pas refait
        here = os.path.dirname(os.path.abspath(__file__))
        for s in ("session_launch.py", "boot_confirm.py"):
            p = os.path.join(here, s)
            if os.path.isfile(p):
                age_h = (time.time() - os.path.getmtime(p)) / 3600.0
                if age_h < 24:
                    rep.warn(f"  [!] {s} modifie il y a {age_h:.1f} h mais sfs NON "
                             f"regenere -> ta modif N'IRA PAS au boot sans --rootfs-src.")
    built = run_build(a.config, rep, src=a.src, infra_conf=a.infra)
    kver = detect_kver(a.src)
    write_report(rep, kver=kver,
                 result=("build-ok" if built else "build-echoue"))
    if not built:
        print("!! build echoue (cf. sortie kernel_build). Rien arme.", flush=True)
        _push_failure(rep, repo, "build echoue")
        stop_stream(stream)
        sys.exit(3)

    print(">> demande d'autorisation (git/local)...", flush=True)
    authorized = request_git_authorization(rep, repo=repo, kver=kver,
                                           bypass=a.bypass_validation)
    write_report(rep, kver=kver,
                 result=("finalise" if authorized else "differe"))
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


def write_report(rep, kver=None, result=None):
    """Ecrit le rapport first-boot DANS le manager (meme racine que le registre :
    MANAGER_ROOT, lui-meme issu d'infra.conf [manager] -- aucun chemin code en
    dur) et journalise un evenement COHERENT (kind EV_FIRSTBOOT) rattache au
    kver, comme les autres parties du logiciel. 'result' : None (empreinte) ou
    'build-ok' / 'build-echoue' / 'finalise' / 'differe'."""
    root = os.environ.get("MANAGER_ROOT", "/boot_pool/manager")
    reg = None
    try:
        import kernel_registry
        reg = kernel_registry.KernelRegistry()
        root = str(reg.root)          # source unique de la racine du manager
    except Exception:
        pass
    path = os.path.join(root, "first-boot-report.txt")
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "w") as f:
            f.write(rep.text())
    except OSError:
        pass
    # journal append-only du manager : evenement first-boot rattache au kver
    if reg is not None:
        try:
            kind = getattr(kernel_registry, "EV_FIRSTBOOT", "first-boot")
            detail = (f"result={result or 'empreinte'} "
                      f"crit={len(rep.criticals)} warn={len(rep.warnings)}")
            reg.log_event(kind, kver, detail)
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

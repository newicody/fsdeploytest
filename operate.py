#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
operate.py -- point d'entree d'exploitation UNIQUE (le travailleur), utilisable
dans LES TROIS contextes (chroot / systeme booted / mode rescue). Il lit
l'infra.conf PHYSIQUEMENT present sur la machine (/etc/infra.conf, ou --infra) et
s'adapte au contexte detecte.

operate fait TOUT le travail de maintenance : il SUBSUME first_boot (commande
'deploy') et expose chaque fonctionnalite via la CLI. Les fonctions reutilisables
de first_boot (verify_infra/preflight/Report) restent importees comme une
bibliotheque ; first_boot n'est plus une porte d'entree distincte.

L'INFERENCE est AUTONOME (service de fond dispatch_service.py, pilote par
[dispatch] d'infra.conf) : ce n'est PAS une commande operate.

Contextes et ce qui change :
  - chroot  : depuis un live ; pools peut-etre a importer, ESP/efivars a monter.
  - booted  : production reelle ; racine '/' = OVERLAY vivant, pools/datasets
              deja montes par init.py, inference disponible.
  - rescue  : shell de maintenance (overlay non actif / initramfs degrade) ;
              on fait au mieux avec ce qui est monte.
operate NE refuse aucun contexte : il le DETECTE, l'affiche, previent si un
prerequis manque (ex : fast_pool non importe), sans bloquer.

Dispatcher MINCE : aucune logique metier dupliquee. Chaque sous-commande delegue
au module existant (meme codebase), appele en sous-processus avec INFRA_CONF
exporte. operate n'ajoute QUE la logique d'environnement (detection contexte,
montage ESP/efivars, restage initramfs multi-ESP) et les enchainements (rootfs
complet, restore) qui evitent de dupliquer le bootstrap.

Sous-commandes :
  status        etat env + contexte + conformite (reutilise verify_infra)
  check         suite de saintete READ-ONLY (infra + sfs/ESP montage RO test) ;
                --strict -> rc!=0 si critique ; --diagnose -> + kernel_diagnose
  deploy        BOOTSTRAP complet (ex-first_boot) : datasets + source + .config +
                build noyau + rootfs + registre. [--config F] [--rootfs-src D] ...
  kernel        rebuild COMPLET noyau+modules+initramfs+ESP+EFI -> kernel_build.py
                recree TOUTES les entrees EFI par profil [uki]. [--config X]
  rootfs        REBUILD complet d'un rootfs.sfs : sans source -> nettoie la racine
                vivante (clean_rootfs -> fast_pool/staging) puis fige rootfs-vN.sfs
                + (re)pointe le lien. [--source D] [--rootfs-src D|--modules KVER
                -> passthrough sfs_build]
  initramfs     rebuild de l'initramfs SEUL + restage sur TOUTES les ESP. [--kver V]
  restore       remet en place un rootfs depuis un .sfs : --sfs FICHIER -> importe
                comme rootfs-vN.sfs + active le lien (effet au prochain boot).
  config        propose/applique une maj du .config             -> kernel_watch.py
  config-history  historique des .config (list|render|show)      -> config_history.py
  config-delta    compare deux .config                            -> config_delta.py
  diagnose      coherence noyau/modules/sfs                       -> kernel_diagnose.py
  source        cycle de vie des sources (list|fetch|select)      -> source_manager.py
  freeze        fige l'overlay vivant en rootfs-vN.sfs            -> freeze_overlay.py
  select        list | use <vN> | rollback (lien rootfs.sfs)      -> select_rootfs.py
  clean         prepare une racine nettoyee pour le sfs           -> clean_rootfs.py
  snapshot      gestion des snapshots ZFS                         -> snapshot_manager.py
  replicate     replication incrementale (defaut --from-config)   -> zfs_replicate.py
  storage       audit de conformite stockage                      -> storage_manager.py
  validate      valide SFS + ESP avant de (re)booter              -> validate_boot.py
  confirm       promeut le noyau frais boote (BootOrder)          -> boot_confirm.py
  manager       registre noyaux : list|audit|history|mark|promote -> kernel_registry.py
  manager-sync  synchro git de l'audit trail                      -> manager_git.py
  bench         inventaire + bench machine                        -> machine_bench.py
  brainstorm    flux d'idees local (inference)                    -> brainstorm.py
  rag           RAG multi-domaines local (inference)              -> rag.py
  esp           monte les ESP a leur install_mount (boot_layout)

Options globales (AVANT la sous-commande) : --infra PATH, --comment "texte"
(--comment est journalise et reserve a un futur post sur le mode projet).
Tout argument apres la sous-commande est transmis tel quel au module cible.
stdlib uniquement.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# convention de chemin sur l'ESP, miroir de kernel_build.DEST_DIR
ESP_DEST_DIR = "EFI/gentoo"

# commentaire optionnel (--comment), journalise et reserve au futur post projet.
COMMENT = ""


def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return paths[-1]


# infra.conf PHYSIQUE de la machine : INFRA_CONF (env) > /etc/infra.conf (depose
# par sfs_build, present en booted et en chroot-cible) > copie du depot. En
# rescue, passe --infra si besoin.
INFRA = _first_existing(os.environ.get("INFRA_CONF"),
                        "/etc/infra.conf",
                        os.path.join(HERE, "infra.conf"))


def msg(s):
    print(f"[operate] {s}", flush=True)


# --------------------------------------------------------------------------- #
# detection du contexte (informative, JAMAIS bloquante)
# --------------------------------------------------------------------------- #
def in_chroot():
    """Vrai si la racine de PID 1 differe de la notre (cas chroot depuis un
    live). En booted, PID 1 (session_launch) partage notre racine."""
    try:
        a = os.stat("/")
        b = os.stat("/proc/1/root")
        return (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino)
    except OSError:
        return False


def root_fstype():
    fs = "?"
    try:
        with open("/proc/mounts") as f:
            for ln in f:
                p = ln.split()
                if len(p) >= 3 and p[1] == "/":
                    fs = p[2]
    except OSError:
        pass
    return fs


def pools_imported():
    try:
        out = subprocess.run(["zpool", "list", "-H", "-o", "name"],
                             capture_output=True, text=True).stdout
        return set(out.split())
    except OSError:
        return set()


def detect_context():
    if in_chroot():
        return "chroot"
    if root_fstype() == "overlay":
        return "booted"
    return "rescue"


def report_context():
    """Affiche le contexte detecte et previent des prerequis manquants. Ne
    bloque jamais (operate s'utilise en chroot, booted ET rescue)."""
    ctx = detect_context()
    msg(f"contexte : {ctx}  (racine '/' = {root_fstype()}, infra = {INFRA})")
    if "fast_pool" not in pools_imported():
        msg("ATTENTION : fast_pool non importe -- 'zpool import -N fast_pool' "
            "sinon les operations sfs/rootfs echoueront.")
    return ctx


def ensure_efivars():
    """Monte efivarfs si besoin (requis par efibootmgr : kernel, confirm)."""
    p = "/sys/firmware/efi/efivars"
    if os.path.ismount(p):
        return True
    if not os.path.isdir("/sys/firmware/efi"):
        msg("pas en UEFI (/sys/firmware/efi absent) : efibootmgr indisponible.")
        return False
    subprocess.run(["mount", "-t", "efivarfs", "efivarfs", p],
                   stderr=subprocess.DEVNULL)
    ok = os.path.ismount(p)
    msg("efivarfs monte." if ok else "echec montage efivarfs.")
    return ok


def mount_esps():
    """Monte chaque ESP declaree a son install_mount (vfat) et retourne
    [(name, mountpoint), ...]. L'ESP n'est pas montee au runtime."""
    try:
        import boot_layout
        esps = boot_layout.load_esps(INFRA)
    except Exception as e:
        msg(f"boot_layout indisponible ({e}) : montage ESP saute.")
        return []
    if not esps:
        msg("aucune ESP declaree dans [efi].")
        return []
    out = []
    for e in esps:
        mp = e.mount_for_install(log=lambda m: msg("  " + m))
        if mp:
            out.append((e.name, mp))
            msg(f"ESP {e.name} -> {mp}")
        else:
            msg(f"ESP {e.name} : montage impossible ({getattr(e, 'reason', '?')})")
    return out


# --------------------------------------------------------------------------- #
# journalisation via le manager (kernel_registry -> boot_pool/manager)
# --------------------------------------------------------------------------- #
def journal(kind, detail):
    """Ecrit un evenement dans le manager (boot_pool/manager/history.jsonl) via
    kernel_registry. Best-effort : si le manager est indisponible (chroot/rescue,
    boot_pool non monte), on n'echoue JAMAIS l'operation pour autant. Le
    commentaire global (--comment) est joint au detail si present."""
    if COMMENT:
        detail = f"{detail} | comment: {COMMENT}"
    try:
        import kernel_registry
        kernel_registry.KernelRegistry().log_event(kind, None, detail)
        return True
    except Exception:
        return False


def _zfs_mountpoint(ds):
    try:
        p = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint", ds],
                           capture_output=True, text=True).stdout.strip()
        return p if p not in ("", "legacy", "none", "-") else ""
    except OSError:
        return ""


def _sfs_dir():
    """Repertoire ou vit rootfs.sfs (suit le mountpoint reel de fast_pool/sfs)."""
    for cand in (_zfs_mountpoint("fast_pool/sfs"), "/fast_pool/sfs", "/mnt/sfs"):
        if cand and os.path.isdir(cand):
            return cand
    return ""


# --------------------------------------------------------------------------- #
# delegation aux modules existants
# --------------------------------------------------------------------------- #
def _load_github_token():
    """Charge GITHUB_TOKEN depuis [manager] token_file (sinon /etc/github.token,
    sinon <MANAGER_ROOT>/github.token), comme session_launch. operate est un
    process SEPARE : il n'herite pas du token charge par session_launch au boot.
    Sans ca, la remontee git de fin de commande ET les push board des modules
    delegues sont silencieusement sautes hors booted. N'ecrase pas un token deja
    present. Best-effort."""
    if os.environ.get("GITHUB_TOKEN"):
        return
    cands = []
    try:
        from configobj import ConfigObj
        m = ConfigObj(INFRA).get("manager", {}) or {}
        if m.get("token_file"):
            cands.append(m["token_file"])
        if m.get("root"):
            cands.append(os.path.join(m["root"], "github.token"))
    except Exception:
        pass
    cands += ["/etc/github.token", "/boot_pool/manager/github.token"]
    for f in cands:
        try:
            if f and os.path.isfile(f):
                tok = open(f).read().strip()
                if tok:
                    os.environ["GITHUB_TOKEN"] = tok
                    return
        except OSError:
            pass


def run_module(script, args):
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        sys.exit(f"module introuvable : {path} (operate.py doit etre dans le "
                 "depot, a cote de ses modules).")
    env = dict(os.environ, INFRA_CONF=INFRA)
    return subprocess.run([sys.executable, path] + list(args), env=env).returncode


def kernel_src():
    try:
        from configobj import ConfigObj
        return (ConfigObj(INFRA).get("kernel", {}).get("src")
                or "/usr/src/linux")
    except Exception:
        return "/usr/src/linux"


# sous-commandes en simple passthrough vers leur module (memes args/CLI)
PASS = {
    # --- noyau & config ---
    "config":         "kernel_watch.py",
    "source":         "source_manager.py",
    "diagnose":       "kernel_diagnose.py",
    "config-history": "config_history.py",
    "config-delta":   "config_delta.py",
    # --- rootfs / overlay (rootfs = handler natif cmd_rootfs) ---
    "freeze":     "freeze_overlay.py",
    "select":     "select_rootfs.py",
    "clean":      "clean_rootfs.py",
    # --- zfs / stockage ---
    "snapshot":   "snapshot_manager.py",
    "storage":    "storage_manager.py",
    "validate":   "validate_boot.py",
    # --- manager (registre noyaux + audit trail git) ---
    "manager":      "kernel_registry.py",
    "manager-sync": "manager_git.py",
    # --- outils ---
    "bench":      "machine_bench.py",
    "brainstorm": "brainstorm.py",
    "rag":        "rag.py",
}
# NB: 'dispatch' n'est PAS une commande : l'inference est AUTONOME
# (dispatch_service.py, [dispatch] d'infra.conf). Debug a la main hors operate :
# 'python3 dispatch.py'.

# commandes ne touchant pas au systeme -> root non requis (check : mode degrade
# sans root, controle complet avec root)
NO_ROOT = {"status", "rag", "brainstorm", "check",
           "config-history", "config-delta", "manager", "manager-sync"}


# --------------------------------------------------------------------------- #
# handlers specifiques
# --------------------------------------------------------------------------- #
def cmd_status(rest):
    efivars = "monte" if os.path.ismount("/sys/firmware/efi/efivars") else "non monte"
    msg(f"infra      : {INFRA}")
    msg(f"contexte   : {detect_context()}")
    msg(f"racine '/' : {root_fstype()}  (overlay attendu en booted)")
    msg(f"pools      : {', '.join(sorted(pools_imported())) or '(aucun)'}")
    msg(f"efivars    : {efivars}")
    try:
        import first_boot
        from configobj import ConfigObj
        rep = first_boot.Report()
        first_boot.verify_infra(ConfigObj(INFRA), rep)
        print(rep.text(), flush=True)
    except Exception as e:
        msg(f"verify_infra indisponible : {e}")
    return 0


def cmd_check(rest):
    """Suite de saintete READ-ONLY : agrege les verificateurs existants sans
    rien modifier (les montages sont RO + auto-demontes ; seule ecriture = la
    ligne de journal). --strict -> rc != 0 si un critique echoue (garde-fou
    scriptable). Journalise le verdict dans le manager."""
    ctx = report_context()
    strict = "--strict" in rest
    do_diag = "--diagnose" in rest
    crit = warn = 0

    print("== 1. conformite infra (reel vs declare) ==")
    try:
        import first_boot
        from configobj import ConfigObj
        rep = first_boot.Report()
        first_boot.verify_infra(ConfigObj(INFRA), rep)
        print(rep.text())
        crit += len(rep.criticals)
        warn += len(rep.warnings)
    except Exception as e:
        print(f"  verify_infra indisponible : {e}")
        warn += 1

    print("== 2. artefacts SFS actifs + ESP (montage RO test) ==")
    if os.geteuid() != 0:
        print("  (non-root : montages RO test SAUTES ; relance en root pour le "
              "controle complet des sfs/ESP)")
        warn += 1
    else:
        try:
            import validate_boot
            import boot_layout
            kver = os.uname().release
            sfs_dir = _sfs_dir()
            sfs_list = []
            if sfs_dir:
                sfs_list.append((os.path.join(sfs_dir, "rootfs.sfs"), "rootfs"))
                mods = os.path.join(sfs_dir, f"modules-{kver}.sfs")
                if os.path.exists(mods):
                    sfs_list.append((mods, "modules"))
                else:
                    print(f"  [warn] modules-{kver}.sfs absent de {sfs_dir} "
                          "(cohrence kver initramfs/modules a verifier)")
                    warn += 1
            else:
                print("  dataset sfs introuvable/non monte -> SFS non verifies")
                warn += 1
            esp_parts = []
            try:
                for e in boot_layout.load_esps(INFRA):
                    d = e.device()
                    if d:
                        esp_parts.append(d)
            except Exception as e:
                print(f"  ESP non listees ({e})")
            for c in validate_boot.validate_all(sfs_list=sfs_list,
                                                esp_list=esp_parts):
                if not c.ok:
                    crit += 1
        except Exception as e:
            print(f"  validate_boot indisponible : {e}")
            warn += 1

    if do_diag:
        print("== 3. diagnostic noyau/runtime (kernel_diagnose) ==")
        run_module("kernel_diagnose.py", [])

    verdict = f"{crit} critique(s), {warn} warning(s)"
    print(f"\n== verdict saintete [{ctx}] : {verdict} ==")
    journal("check", f"ctx={ctx} crit={crit} warn={warn} strict={strict}")
    if strict and crit:
        print("STRICT : ECHEC (critiques presents) -> rc=1.")
        return 1
    return 0


def cmd_deploy(rest):
    """BOOTSTRAP complet (ex-first_boot) : operate subsume first_boot. On delegue
    a first_boot.py (datasets + source + .config + build + rootfs + registre) en
    injectant --infra (resolu par operate) si l'appelant ne l'a pas precise. Tous
    les autres arguments (--config, --rootfs-src, --src, --dry-run, --yes...) sont
    transmis tels quels."""
    report_context()
    ensure_efivars()
    args = list(rest)
    if "--infra" not in args and not any(a.startswith("--infra=") for a in args):
        args = ["--infra", INFRA] + args
    return run_module("first_boot.py", args)


def cmd_kernel(rest):
    report_context()
    # option locale --config / -c : installer un .config + olddefconfig avant de
    # deleguer a kernel_build (qui suppose le .config deja en place).
    cfg, expect, passth, it = None, None, [], iter(rest)
    for a in it:
        if a in ("--config", "-c"):
            cfg = next(it, None)
        elif a == "--expect":
            expect = next(it, None)
        else:
            passth.append(a)
    if cfg:
        if not os.path.isfile(cfg):
            sys.exit(f".config introuvable : {cfg}")
        src = kernel_src()
        dst = os.path.join(src, ".config")
        if os.path.abspath(cfg) != os.path.abspath(dst):
            shutil.copy2(cfg, dst)
            msg(f".config installe -> {dst}")
        subprocess.run(["make", "-C", src, "olddefconfig"])
    ensure_efivars()
    mount_esps()
    if expect:
        os.environ["KVER_EXPECT"] = expect   # garde-fou cible<->source (run_module propage)
    return run_module("kernel_build.py", passth)


def cmd_rootfs(rest):
    """REBUILD d'un rootfs.sfs. Deux modes :
      - passthrough : si --rootfs-src ou --modules est fourni, on transmet tel
        quel a sfs_build.py (build depuis un arbre prepare / modules-<ver>.sfs).
      - complet (defaut) : sans source, on REBUILD depuis la racine vivante
        (--source, defaut '/') : clean_rootfs (rsync + nettoyage + marqueur ->
        fast_pool/staging) PUIS sfs_build versionne+lien. C'est 'recompiler un
        rootfs.sfs' en une commande (meme chaine que le bootstrap)."""
    report_context()
    if any(a in ("--rootfs-src", "--modules") for a in rest) \
            or any(a.startswith(("--rootfs-src=", "--modules=")) for a in rest):
        return run_module("sfs_build.py", rest)
    source, extra, it = "/", [], iter(rest)
    for a in it:
        if a == "--source":
            source = next(it, "/")
        elif a.startswith("--source="):
            source = a.split("=", 1)[1]
        else:
            extra.append(a)            # ex --force / --force-live -> sfs_build
    staging = _zfs_mountpoint("fast_pool/staging")
    if not staging:
        msg("fast_pool/staging non monte -> rebuild rootfs impossible "
            "(zfs mount fast_pool/staging).")
        return 1
    msg(f"rebuild rootfs : {source} -> nettoyage vers {staging} (clean_rootfs)...")
    rc = run_module("clean_rootfs.py",
                    ["--source", source, "--staging", staging, "--yes"])
    if rc != 0:
        msg("clean_rootfs a echoue -> rebuild avorte (rien fige).")
        return rc
    msg("fige du rootfs nettoye en rootfs-vN.sfs + lien (sfs_build)...")
    return run_module("sfs_build.py", ["--rootfs-src", staging] + extra)


def cmd_initramfs(rest):
    """Rebuild de l'initramfs SEUL puis restage sur TOUTES les ESP. Pas de
    recompilation noyau. Les entrees EFI existantes pointent vers le meme
    'initramfs-<kver>.zst' -> tous les profils (normal/safe/debug/i915) prennent
    le nouvel initramfs sans recreer d'entree NVRAM."""
    report_context()
    kver = os.uname().release
    passth, it = [], iter(rest)
    for a in it:
        if a == "--kver":
            kver = next(it, kver)
        else:
            passth.append(a)
    out_path = os.path.join("/var/tmp", f"initramfs-{kver}.zst")
    # build_initramfs ecrit OUT ; KVER selectionne les modules/zfs.ko embarques.
    env = dict(os.environ, INFRA_CONF=INFRA, KVER=kver, OUT=out_path)
    bscript = os.path.join(HERE, "build_initramfs.py")
    if not os.path.isfile(bscript):
        sys.exit(f"module introuvable : {bscript}")
    rc = subprocess.run([sys.executable, bscript] + passth, env=env).returncode
    if rc != 0:
        msg("build_initramfs a ECHOUE -> ESP NON modifiee (pas de vieux fichier "
            "stage).")
        return rc
    if not os.path.isfile(out_path):
        msg(f"initramfs attendu absent : {out_path}")
        return 1
    esps = mount_esps()
    if not esps:
        msg("aucune ESP montee -> initramfs construit mais NON stage "
            f"({out_path}). Copie-le manuellement.")
        return 1
    staged = 0
    for name, mp in esps:
        dst_dir = os.path.join(mp, ESP_DEST_DIR)
        if not os.path.isdir(dst_dir):
            msg(f"  {name}: {dst_dir} absent (noyau jamais stage ici ?) -> saute")
            continue
        dst = os.path.join(dst_dir, f"initramfs-{kver}.zst")
        shutil.copy2(out_path, dst)
        msg(f"  {name}: initramfs-{kver}.zst mis a jour")
        staged += 1
    if staged:
        msg(f"OK : initramfs restage sur {staged} ESP. TOUTES les entrees EFI "
            f"(tous profils) referencent ce fichier -> prises en compte. Reboot.")
        return 0
    msg("aucune ESP n'avait le dossier de staging : rien copie.")
    return 1


def cmd_restore(rest):
    """Remet en place un rootfs depuis un .sfs (ex : sauvegarde, image de
    secours). --sfs FICHIER : si le fichier est deja dans le dataset sfs, on
    active son lien ; sinon on l'importe comme rootfs-vN.sfs (version suivante)
    puis on (re)pointe rootfs.sfs dessus. Effet au PROCHAIN boot. Reutilise
    select_rootfs (versionnage + lien atomique + memo rollback) : aucune logique
    parallele."""
    report_context()
    sfs, it = None, iter(rest)
    for a in it:
        if a == "--sfs":
            sfs = next(it, None)
        elif a.startswith("--sfs="):
            sfs = a.split("=", 1)[1]
    if not sfs:
        msg("usage : operate restore --sfs <fichier.sfs>")
        return 2
    if not os.path.isfile(sfs):
        msg(f"sfs introuvable : {sfs}")
        return 1
    sfs_dir = _sfs_dir()
    if not sfs_dir:
        msg("dataset fast_pool/sfs introuvable/non monte.")
        return 1
    try:
        import select_rootfs
    except Exception as e:
        msg(f"select_rootfs indisponible : {e}")
        return 1
    target = os.path.basename(sfs)
    if os.path.dirname(os.path.abspath(sfs)) != os.path.abspath(sfs_dir):
        vers = select_rootfs._versions(sfs_dir)
        nextn = (vers[-1][0] + 1) if vers else 1
        target = f"rootfs-v{nextn}.sfs"
        dst = os.path.join(sfs_dir, target)
        msg(f"import du sfs -> {dst}")
        shutil.copy2(sfs, dst)
    select_rootfs._set_link(sfs_dir, target)
    msg(f"rootfs.sfs -> {target}. Effet au prochain boot "
        "('operate validate' puis reboot recommandes).")
    return 0


def cmd_replicate(rest):
    report_context()
    return run_module("zfs_replicate.py", rest or ["--from-config"])


def cmd_confirm(rest):
    report_context()
    ensure_efivars()
    return run_module("boot_confirm.py", rest)


def cmd_esp(rest):
    report_context()
    mount_esps()
    return 0


def cmd_pass(name, rest):
    report_context()
    return run_module(PASS[name], rest)


# --------------------------------------------------------------------------- #
USAGE = (
    "usage: operate.py [--infra PATH] [--comment TXT] <commande> [args...]\n"
    "\n"
    "commandes :\n"
    "  noyau     : deploy kernel config diagnose source config-history config-delta\n"
    "  rootfs    : rootfs restore freeze select clean\n"
    "  boot      : initramfs esp validate confirm\n"
    "  stockage  : snapshot storage replicate\n"
    "  manager   : manager manager-sync\n"
    "  systeme   : status check bench brainstorm rag\n"
    "\n"
    "Utilisable en chroot / booted / rescue (lit l'infra.conf de la machine).\n"
    "Exemples :\n"
    "  operate.py kernel --config K.config        # recompile noyau + init + EFI\n"
    "  operate.py rootfs                          # rebuild rootfs.sfs (depuis '/')\n"
    "  operate.py restore --sfs /chemin/img.sfs   # remet un rootfs en place\n"
    "  operate.py manager audit                   # etat du registre noyaux\n"
    "  operate.py deploy --config K.config --rootfs-src /  # bootstrap complet\n"
    "\n"
    "L'inference est AUTONOME (service de fond, [dispatch] d'infra.conf) : pas de\n"
    "commande ici. --comment est journalise (futur post sur le mode projet).\n"
    "Detail des roles : voir l'entete de ce fichier."
)


def main():
    global INFRA, COMMENT
    argv = sys.argv[1:]

    # options globales EN TETE (decoupage manuel : argparse.REMAINDER ne
    # transmet pas un --flag place en tete du passthrough).
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            print(USAGE)
            return
        if tok == "--infra":
            if i + 1 >= len(argv):
                sys.exit("--infra attend un chemin")
            INFRA = argv[i + 1]
            i += 2
        elif tok.startswith("--infra="):
            INFRA = tok.split("=", 1)[1]
            i += 1
        elif tok == "--comment":
            if i + 1 >= len(argv):
                sys.exit("--comment attend un texte")
            COMMENT = argv[i + 1]
            i += 2
        elif tok.startswith("--comment="):
            COMMENT = tok.split("=", 1)[1]
            i += 1
        else:
            break

    rest_all = argv[i:]
    if not rest_all:
        print(USAGE)
        sys.exit(2)
    cmd, passthrough = rest_all[0], rest_all[1:]

    handlers = {
        "status":    cmd_status,
        "check":     cmd_check,
        "deploy":    cmd_deploy,
        "kernel":    cmd_kernel,
        "rootfs":    cmd_rootfs,
        "initramfs": cmd_initramfs,
        "restore":   cmd_restore,
        "esp":       cmd_esp,
        "replicate": cmd_replicate,
        "confirm":   cmd_confirm,
    }
    if cmd not in handlers and cmd not in PASS:
        sys.exit(f"commande inconnue : {cmd}\n\n{USAGE}")

    if os.geteuid() != 0 and cmd not in NO_ROOT:
        sys.exit(f"root requis pour '{cmd}'.")

    # manager : aligne MANAGER_ROOT sur infra.conf [manager] (env > config) pour
    # que le journal d'operate ET les modules delegues (kernel_build, boot_confirm)
    # pointent au MEME manager. run_module propage os.environ.
    if "MANAGER_ROOT" not in os.environ:
        try:
            from configobj import ConfigObj
            mr = (ConfigObj(INFRA).get("manager", {}) or {}).get("root")
            if mr:
                os.environ["MANAGER_ROOT"] = mr
        except Exception:
            pass

    # TOKEN github : indispensable pour que la remontee git (fin de commande) ET
    # les push board des modules delegues fonctionnent hors booted. operate ne
    # l'herite pas de session_launch (process separe) -> on le charge ici.
    _load_github_token()

    detail = (cmd + (" " + " ".join(passthrough) if passthrough else "")).strip()
    journal("operate", f"start: {detail}")
    if cmd in handlers:
        rc = handlers[cmd](passthrough)
    else:
        rc = cmd_pass(cmd, passthrough)
    journal("operate", f"end: {detail} -> rc={rc}")
    # REMONTEE GIT : pousser l'audit trail (journal de cette operation) vers le
    # remote du manager. Frontiere d'operation -> push une fois par commande.
    # Best-effort : n'affecte jamais le rc de l'operation.
    try:
        import manager_git
        manager_git.sync(f"operate {detail} (rc={rc})", push=True)
    except Exception:
        pass
    sys.exit(rc)


if __name__ == "__main__":
    main()

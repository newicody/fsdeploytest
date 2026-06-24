#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
/sbin/session_launch.py — execute apres switch_root, dans le rootfs Gentoo
(PID 1). Monte le minimum, lance le compositeur wlroots kiosk (cage), et
bascule le stream de fbdev (initramfs) vers la capture wayland.

Dependances rootfs : seatd, cage (ou sway), foot, ffmpeg, et
wl-screenrec OU wf-recorder.
"""
import os
import subprocess
import time

RTMP = "rtmp://a.rtmp.youtube.com/live2"
VBR = "4500k"
RUNTIME_DIR = "/run/user/0"


def log(msg):
    line = f"[session] {msg}\n"
    try:
        with open("/dev/kmsg", "w") as k:
            k.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, **kw)


def mount(src, tgt, fstype):
    os.makedirs(tgt, exist_ok=True)
    subprocess.run(["mount", "-t", fstype, src, tgt],
                   stderr=subprocess.DEVNULL)


def read_key():
    try:
        with open("/etc/yt.key") as f:
            return f.read().strip()
    except OSError:
        return ""


def stop_initramfs_stream():
    pidf = "/etc/initramfs-stream.pid"
    try:
        with open(pidf) as f:
            pid = int(f.read().strip())
        os.kill(pid, 15)
        os.remove(pidf)
        log("stream fbdev arrete (bascule wayland)")
    except (OSError, ValueError):
        pass


def start_wayland_stream(key):
    """Attend le socket wayland puis capture l'ecran vers RTMP YouTube.
    Diagnostiquable : ecrit les erreurs ffmpeg/recorder dans /var/log/stream.log
    (au lieu de les jeter), verifie les prerequis (cle, outil, VAAPI)."""
    if not key:
        log("[stream] pas de cle YouTube (/etc/yt.key) -> stream desactive. "
            "Pour activer : ecris ta cle de diffusion dans /etc/yt.key")
        return
    sock = os.path.join(RUNTIME_DIR, "wayland-0")
    for _ in range(40):                        # attendre cage (jusqu'a 20 s)
        if os.path.exists(sock):
            break
        time.sleep(0.5)
    if not os.path.exists(sock):
        log(f"[stream] socket wayland absent ({sock}) apres 20 s -> pas de capture")
        return
    env = dict(os.environ, WAYLAND_DISPLAY="wayland-0", XDG_RUNTIME_DIR=RUNTIME_DIR)
    url = f"{RTMP}/{key}"
    # log dedie : on NE jette PLUS stderr (sinon impossible de diagnostiquer).
    os.makedirs("/var/log", exist_ok=True)
    logf = open("/var/log/stream.log", "ab", buffering=0)
    vaapi = os.path.exists("/dev/dri/renderD128")
    log(f"[stream] demarrage (VAAPI={'oui' if vaapi else 'non'}, "
        f"log: /var/log/stream.log)")

    if which("wl-screenrec") and vaapi:        # Intel VAAPI : encodage materiel
        log("[stream] wl-screenrec (HEVC VAAPI) -> YouTube")
        subprocess.Popen(
            ["wl-screenrec", "--codec", "hevc", "--ffmpeg-muxer", "flv", "-f", url],
            env=env, stdout=logf, stderr=logf)
    elif which("wf-recorder"):
        log("[stream] wf-recorder + ffmpeg (x264 logiciel) -> YouTube")
        rec = subprocess.Popen(["wf-recorder", "-c", "rawvideo", "-f", "-"],
                               env=env, stdout=subprocess.PIPE, stderr=logf)
        subprocess.Popen(
            ["ffmpeg", "-nostdin", "-f", "rawvideo", "-i", "-",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", VBR,
             "-maxrate", VBR, "-bufsize", "9000k", "-pix_fmt", "yuv420p",
             "-g", "60", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-f", "flv", url],
            stdin=rec.stdout, stdout=logf, stderr=logf)
    else:
        log("[stream] AUCUN outil de capture wayland installe "
            "(emerge gui-apps/wl-screenrec ou gui-apps/wf-recorder). "
            "Pas de stream.")
        return
    log("[stream] capture wayland demarree (verifie /var/log/stream.log si rien "
        "n'apparait sur YouTube)")


def which(name):
    for p in os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":"):
        f = os.path.join(p, name)
        if os.access(f, os.X_OK):
            return f
    return None


def degraded_repair():
    """Mode degrade : affiche le rapport et ouvre un shell de reparation au
    lieu de la session normale. Le stream initramfs n'est PAS arrete -> le
    rapport reste visible a distance (YouTube). Ne revient jamais (exec shell)."""
    report = "/etc/degraded-report"
    banner = "\n" + "#" * 64 + "\n# SYSTEME EN MODE DEGRADE / REPARATION\n" + \
             "#" * 64 + "\n"
    try:
        with open(report) as f:
            banner += f.read()
    except OSError:
        banner += "(rapport /etc/degraded-report introuvable)\n"
    banner += ("\nSession normale NON demarree. Shell de reparation.\n"
               "Outils : zpool status -v | zfs list | dmesg | "
               "efibootmgr -v\n" + "#" * 64 + "\n")
    # afficher sur toutes les consoles + kmsg (donc visible dans le stream fbdev)
    for dev in ("/dev/console", "/dev/tty0", "/dev/kmsg"):
        try:
            with open(dev, "w") as d:
                d.write(banner)
        except OSError:
            pass
    print(banner, flush=True)
    # un shell interactif sur la console ; on garde le stream fbdev tel quel
    os.environ.setdefault("PS1", "(reparation) # ")
    sh = which("bash") or which("sh") or "/bin/sh"
    os.execv(sh, [sh])


def _openrc_bin():
    """Chemin du binaire 'openrc' (lance un runlevel), ou '' si absent."""
    return which("openrc") or ("/sbin/openrc" if os.path.exists("/sbin/openrc")
                               else "")


def openrc_bringup():
    """Initialise le RUNTIME OpenRC via les runlevels sysinit puis boot. C'est
    l'etape qui manquait : elle cree /run/openrc (deptree, VERROUS, softlevel),
    monte les pseudo-FS de facon idempotente et demarre sysfs/devfs/udev EN TANT
    QUE SERVICES OpenRC. Sans elle, 'rc-service X start' echoue ('bad file
    descriptor' sur les verrous /run/openrc, 'sysfs would not start', 'devfs
    failed') car la base n'est pas initialisee et aucune dependance n'est
    resolue. Best-effort ; retourne True si openrc a tourne (-> udev gere par
    OpenRC, on n'en lance pas un second a la main)."""
    openrc = _openrc_bin()
    if not openrc:
        log("[openrc] binaire 'openrc' absent -> bring-up MANUEL (fallback)")
        return False
    ran = True
    for level in ("sysinit", "boot"):
        log(f"[openrc] runlevel {level}...")
        try:
            rc = subprocess.run([openrc, level]).returncode
        except OSError as e:
            log(f"[openrc] {level} non lance ({e})")
            ran = False
            continue
        if rc != 0:
            log(f"[openrc] runlevel {level} rc={rc} (deja monte/demarre ? on "
                "continue ; PID 1 ne quitte jamais)")
    return ran


def _locate_infra(infra_path=None):
    """Trouve infra.conf (le sfs l'embarque). Retourne le chemin ou ''."""
    cands = [infra_path] if infra_path else []
    cands += ["/etc/infra.conf", "/infra.conf", "/sbin/infra.conf"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return ""


def ensure_zfs_booted():
    """Apres switch_root, les montages /mnt/* de l'initramfs ont disparu (hors
    NEWROOT) ; les pools restent IMPORTES (etat noyau). On (re)monte les datasets
    du systeme booted a leur mountpoint REEL avec le mecanisme STANDARD, qui
    RESPECTE le contrat 'canmount' d'infra.conf (reflete dans la propriete ZFS) :
      - canmount=on / defaut -> monte (fast_pool/sfs, data_pool/home, ...) ;
      - canmount=noauto       -> SAUTE (fast_pool/rootfs = upper de l'overlay,
                                  fast_pool/staging) : ne JAMAIS auto-monter ;
      - canmount=off          -> SAUTE (reserve, archives conteneur).
    'zfs mount -a' monte dans l'ordre parent->enfant et inclut les enfants
    DYNAMIQUES (data_pool/home/<user> en multi-utilisateur). Idempotent.

    IMPORTANT : on ne fait PAS 'zfs mount <ds>' dataset par dataset -> un montage
    explicite monterait MEME les canmount=noauto (l'upper de l'overlay), ce qui
    casse l'overlay et le check rootfs. 'zfs mount -a' est le bon outil : il EST
    le contrat canmount. La garde de creation utilisateur (ensure_user) verifie
    ensuite que /home est bien data_pool/home avant tout --create-home."""
    if not which("zpool"):
        log("[zfs] zpool absent -> montages booted sautes")
        return
    # filet : importer tout pool encore absent (ex boot_pool). -N = sans monter.
    subprocess.run(["zpool", "import", "-aN"], stderr=subprocess.DEVNULL)
    r = subprocess.run(["zfs", "mount", "-a"], capture_output=True, text=True)
    if r.returncode == 0:
        log("[zfs] datasets booted montes (zfs mount -a ; canmount respecte)")
    else:
        log(f"[zfs] zfs mount -a incomplet : {(r.stderr or '').strip()[:140]}")
    # verification ciblee du CONTRAT au boot booted : fast_pool/sfs (rootfs.sfs
    # lisible -> operate check) et data_pool/home (/home) DOIVENT etre montes.
    # On le SIGNALE sans rien forcer (forcer casserait le contrat canmount).
    for ds in ("fast_pool/sfs", "data_pool/home"):
        g = subprocess.run(["zfs", "get", "-H", "-o", "value", "mounted", ds],
                           capture_output=True, text=True)
        if g.returncode == 0 and g.stdout.strip() == "no":
            log(f"[zfs] ATTENTION : {ds} non monte apres 'zfs mount -a' "
                "(verifier canmount/mountpoint/overlay)")


def load_github_token(infra_path=None):
    """Charge GITHUB_TOKEN depuis un fichier (comme /etc/yt.key pour YouTube),
    sinon TOUT le reporting git est silencieusement saute (board push, synchro
    manager) car le code est garde par os.environ.get('GITHUB_TOKEN'). Source :
    [manager] token_file, sinon /etc/github.token, sinon <MANAGER_ROOT>/github.
    token. Best-effort ; n'ecrase pas un token deja present dans l'env."""
    if os.environ.get("GITHUB_TOKEN"):
        return
    cands = []
    path = _locate_infra(infra_path)
    if path:
        try:
            from configobj import ConfigObj
            m = ConfigObj(path).get("manager", {}) or {}
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
                    log(f"[git] GITHUB_TOKEN charge depuis {f} (reporting actif)")
                    return
        except OSError:
            continue
    log("[git] aucun token GitHub trouve -> reporting git desactive "
        "(deposer le token dans [manager] token_file pour l'activer)")


def run_boot_confirm():
    """Lance boot_confirm en ARRIERE-PLAN apres le socle : health-check du noyau
    fraichement boote -> promotion (efibootmgr + registre) -> remontee git. En
    tache de fond pour ne pas retarder le compositeur. Idempotent : si le noyau
    est deja 'current', promote ne change rien (pas de commit/push). Best-effort.
    Herite de GITHUB_TOKEN/MANAGER_ROOT deja exportes -> la remontee fonctionne."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = ["/usr/local/sbin/boot_confirm.py",
             os.path.join(here, "boot_confirm.py"),
             which("boot_confirm.py") or ""]
    bc = next((c for c in cands if c and os.path.isfile(c)), "")
    if not bc:
        log("[boot-confirm] script introuvable -> promotion non lancee")
        return
    try:
        subprocess.Popen(["python3", bc],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        log(f"[boot-confirm] lance en arriere-plan ({bc})")
    except OSError as e:
        log(f"[boot-confirm] non lance ({e})")


def prepare_syslog_socket():
    """Solution SANS capabilities : syslog-ng tourne en 'logs' mais ne peut pas
    creer /dev/log (dossier /dev = root:root). On lui donne un repertoire qu'il
    POSSEDE (/run/syslog-ng, logs:logs) pour y binder la socket, et on relie
    /dev/log dessus pour les clients (libc syslog()). A poser AVANT
    start_services (devtmpfs+/run neufs a chaque boot). Best-effort.

    IMPORTANT cote conf : la source syslog-ng doit binder /run/syslog-ng/log
    (et non /dev/log). Sinon un system() qui fait unlink(/dev/log) avant bind
    retire le lien et retombe sur l'EACCES dans /dev. /dev/log reste le point
    d'entree des CLIENTS (eux ne font que connect()). Voir README."""
    import grp
    import pwd
    try:
        uid = pwd.getpwnam("logs").pw_uid
        gid = grp.getgrnam("logs").gr_gid
    except KeyError:
        log("[syslog] user/group 'logs' absent -> socket non preparee")
        return
    d = "/run/syslog-ng"
    try:
        os.makedirs(d, exist_ok=True)
        os.chown(d, uid, gid)
        os.chmod(d, 0o755)
    except OSError as e:
        log(f"[syslog] {d} non prepare ({e})")
        return
    try:
        if os.path.islink("/dev/log") or os.path.exists("/dev/log"):
            os.remove("/dev/log")
        os.symlink("/run/syslog-ng/log", "/dev/log")
        log("[syslog] /run/syslog-ng (logs:logs) pret ; /dev/log -> "
            "/run/syslog-ng/log")
    except OSError as e:
        log(f"[syslog] lien /dev/log non pose ({e})")


def setup_dev():
    """Complete /dev apres le montage devtmpfs (minimal). Le devtmpfs herite de
    l'initramfs n'a NI /dev/fd NI /dev/dri (GPU) NI les liens standards. Sans ca :
      - bash process substitution casse ('broken /dev/fd')
      - emerge refuse ('failed to validate a sane /dev')
      - cage/wlroots ne trouve pas /dev/dri/card0 -> 'unable to create backend'
    On cree les liens standards PUIS on lance eudev (udevd) qui peuple
    dynamiquement /dev (cree /dev/dri/cardN quand le module GPU est charge,
    applique permissions/groupes video/render)."""
    # 1. liens standards vers /proc (indispensables : /dev/fd, stdin/out/err)
    links = {"/dev/fd": "/proc/self/fd",
             "/dev/stdin": "/proc/self/fd/0",
             "/dev/stdout": "/proc/self/fd/1",
             "/dev/stderr": "/proc/self/fd/2",
             "/dev/core": "/proc/kcore"}
    for dst, src in links.items():
        try:
            if not os.path.lexists(dst):
                os.symlink(src, dst)
        except OSError as e:
            log(f"  [!] lien {dst} -> {src} : {e}")
    # 2. /dev/shm (memoire partagee : requis par beaucoup d'apps, dont Mesa)
    os.makedirs("/dev/shm", exist_ok=True)
    subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=1777,nosuid,nodev",
                    "shm", "/dev/shm"], stderr=subprocess.DEVNULL)

    # 3. udev : peuple /dev (cree /dev/dri/cardN, applique perms video/render).
    #    Si OpenRC est present, c'est SON service 'udev' (lance par
    #    openrc_bringup -> runlevel boot) qui s'en charge : on ne lance PAS un
    #    second udevd a la main (cause du conflit 'devfs failed' / double daemon).
    #    Sinon (fallback sans OpenRC), on lance eudev directement.
    if _openrc_bin():
        log("udev delegue a OpenRC (service udev du runlevel boot)")
        return
    udevd = which("udevd") or "/sbin/udevd" if os.path.exists("/sbin/udevd") \
        else (which("systemd-udevd") or "")
    if not udevd:
        # chemins usuels eudev/udev
        for cand in ("/lib/systemd/systemd-udevd", "/usr/lib/systemd/systemd-udevd",
                     "/sbin/udevd", "/usr/sbin/udevd", "/lib/udev/udevd"):
            if os.path.exists(cand):
                udevd = cand
                break
    if udevd:
        log(f"demarrage eudev ({udevd}) pour peupler /dev (GPU, etc.)")
        subprocess.Popen([udevd, "--daemon"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        ua = which("udevadm") or "/bin/udevadm"
        if os.path.exists(ua) or which("udevadm"):
            # declenche la creation des devices pour le materiel deja present
            subprocess.run([ua, "trigger", "--type=devices", "--action=add"],
                           stderr=subprocess.DEVNULL)
            subprocess.run([ua, "settle", "--timeout=10"],
                           stderr=subprocess.DEVNULL)
        log("eudev : /dev peuple (settle termine)")
    else:
        log("[!] udevd/eudev INTROUVABLE : /dev restera minimal "
            "(/dev/dri absent -> pas d'affichage GPU). "
            "Installe sys-fs/eudev dans le rootfs.")
    # 4. filet : si le GPU n'a toujours pas de device, tenter de charger le module
    if not os.path.exists("/dev/dri/card0"):
        for mod in ("i915", "xe"):
            subprocess.run(["modprobe", mod], stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        if which("udevadm"):
            subprocess.run(["udevadm", "trigger", "--subsystem-match=drm",
                            "--action=add"], stderr=subprocess.DEVNULL)
            subprocess.run(["udevadm", "settle", "--timeout=5"],
                           stderr=subprocess.DEVNULL)
    log(f"/dev/dri/card0 present apres setup : {os.path.exists('/dev/dri/card0')}")


def locale_available(loc):
    """La locale est-elle GENEREE dans le rootfs ? (sinon LANG=... -> warnings
    'cannot set LC_*'). On interroge 'locale -a'."""
    try:
        out = subprocess.run(["locale", "-a"], capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    norm = loc.lower().replace("-", "").replace("_", "")
    for line in out.splitlines():
        if line.lower().replace("-", "").replace("_", "") == norm:
            return True
    return False


def setup_environment():
    """Initialise l'environnement systeme + Wayland pour PID 1 et ses enfants
    (seatd, cage, et XWayland plus tard). On NE source PAS /etc/profile en bloc
    (concu pour un shell de login, inadapte/risque pour PID 1) : on definit
    EXPLICITEMENT ce dont le compositeur a besoin. Les shells interactifs
    (maintenance, foot) sourceront /etc/profile via 'sh -l'."""
    # 1. base systeme (ce que /etc/profile fournit). root, pas de home perso ici.
    base = {
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "SHELL": "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh",
        "TERM": os.environ.get("TERM", "linux"),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_CACHE_HOME": "/root/.cache",   # cache shader Mesa, etc.
        "XDG_CONFIG_HOME": "/root/.config",
        "XDG_DATA_HOME": "/root/.local/share",
    }
    for k, v in base.items():
        os.environ.setdefault(k, v)
    os.makedirs("/root/.cache", exist_ok=True)

    # 2. locale fr_FR.UTF-8 SI generee, sinon repli C.UTF-8 (evite les warnings
    #    'cannot set LC_*' quand la locale n'est pas compilee dans le rootfs).
    want = "fr_FR.UTF-8"
    if locale_available(want):
        loc = want
    elif locale_available("C.UTF-8"):
        loc = "C.UTF-8"
        log(f"[!] {want} non generee dans le rootfs -> repli C.UTF-8. "
            f"(genere-la : echo 'fr_FR.UTF-8 UTF-8' >> /etc/locale.gen ; locale-gen)")
    else:
        loc = "C"
        log("[!] ni fr_FR.UTF-8 ni C.UTF-8 generees -> LANG=C (ASCII).")
    os.environ["LANG"] = loc
    os.environ["LC_ALL"] = loc

    # 3. Wayland : type de session + bureau (utile a cage, XWayland, portails).
    #    XDG_RUNTIME_DIR est defini juste apres (depend de RUNTIME_DIR).
    os.environ.setdefault("XDG_SESSION_TYPE", "wayland")
    os.environ.setdefault("XDG_CURRENT_DESKTOP", "cage")
    os.environ.setdefault("MOZ_ENABLE_WAYLAND", "1")   # si firefox un jour
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
    os.environ.setdefault("GDK_BACKEND", "wayland,x11")
    os.environ.setdefault("SDL_VIDEODRIVER", "wayland")
    # XWayland (a venir) : Xwayland exporte DISPLAY=:0 ; on ne le force pas ici.

    log(f"environnement initialise (LANG={loc}, XDG_SESSION_TYPE=wayland)")


def start_services(infra_path="/etc/infra.conf"):
    """Demarre les services OpenRC listes dans [services] de infra.conf, DANS
    L'ORDRE de declaration, via 'rc-service <nom> start'. Format valeur :
    'enabled[,required]' ou 'disabled'. 'required' echoue -> signale fort (mais
    ne bloque jamais : PID 1 doit vivre). Retourne (ok:list, failed:list)."""
    if not os.path.exists(infra_path):
        # repli : chemins usuels (le sfs embarque infra.conf pour les checkups)
        for c in ("/etc/infra.conf", "/infra.conf", "/sbin/infra.conf"):
            if os.path.exists(c):
                infra_path = c
                break
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(infra_path)
        services = cfg.get("services", {})
    except Exception as e:
        log(f"[services] infra.conf illisible ({e}) -> aucun service demarre")
        return [], []
    if not which("rc-service"):
        log("[services] rc-service introuvable (OpenRC absent ?) -> "
            "aucun service demarre")
        return [], []
    ok, failed = [], []
    for name, spec in services.items():
        spec_s = spec if isinstance(spec, str) else ", ".join(spec)
        opts = [o.strip() for o in spec_s.split(",")]
        state = opts[0].lower() if opts else "disabled"
        required = "required" in opts
        if state != "enabled":
            continue
        log(f"[services] demarrage {name}" + (" (requis)" if required else ""))
        try:
            r = subprocess.run(["rc-service", name, "start"],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  [!] {name} : exception {e}")
            failed.append(name)
            continue
        if r.returncode == 0:
            ok.append(name)
            log(f"  {name} demarre OK")
        else:
            failed.append(name)
            tail = (r.stderr or r.stdout or "").strip()[:200]
            sev = "ECHEC REQUIS" if required else "echec"
            log(f"  [!] {name} {sev} (rc={r.returncode}) : {tail}")
    log(f"[services] socle : {len(ok)} demarre(s), {len(failed)} en echec")
    return ok, failed


def _read_session_config(infra_path="/etc/infra.conf"):
    """Lit [session] d'infra.conf. Retourne un dict avec defauts surs."""
    defaults = {
        "user": "appliance",
        "groups": ["video", "input", "seat", "render"],
        "app": "foot",
        "app_fallback": "foot",
        "lock_enabled": False,
        "lock_idle": 300,
        "lock_backend": "swaylock",
        "lock_options": ["--daemonize"],
    }
    if not os.path.exists(infra_path):
        for c in ("/etc/infra.conf", "/infra.conf", "/sbin/infra.conf"):
            if os.path.exists(c):
                infra_path = c
                break
    try:
        from configobj import ConfigObj
        cfg = ConfigObj(infra_path)
        s = cfg.get("session", {})
    except Exception as e:
        log(f"[session] infra.conf illisible ({e}) -> session par defaut")
        return defaults
    if not s:
        return defaults

    def _list(v, d):
        if v is None:
            return d
        return v if isinstance(v, list) else [x.strip() for x in v.split(",")]

    lock = s.get("lock", {})
    return {
        "user": s.get("user", defaults["user"]),
        "groups": _list(s.get("groups"), defaults["groups"]),
        "app": s.get("app", defaults["app"]),
        "app_fallback": s.get("app_fallback", defaults["app_fallback"]),
        "lock_enabled": str(lock.get("enabled", "false")).lower() == "true",
        "lock_idle": int(lock.get("idle_timeout", defaults["lock_idle"]) or 0),
        "lock_backend": lock.get("backend", defaults["lock_backend"]),
        "lock_options": _list(lock.get("options"), defaults["lock_options"]),
    }


def _home_dataset_ready(home_ds="data_pool/home", mp="/home"):
    """Garantit que `mp` (=/home) est bien le dataset `home_ds` MONTE, et pas
    l'overlay racine. Le monte au besoin via zfs_mounts. Retourne True seulement
    si c'est confirme. Empeche useradd --create-home de polluer la racine."""
    try:
        import zfs_mounts
    except Exception:
        # repli minimal : /proc/mounts doit montrer home_ds sur mp
        try:
            with open("/proc/mounts") as f:
                for ln in f:
                    p = ln.split()
                    if len(p) >= 2 and p[0] == home_ds and p[1] == mp:
                        return True
        except OSError:
            pass
        subprocess.run(["zfs", "mount", home_ds], stderr=subprocess.DEVNULL)
        return os.path.ismount(mp)
    st = zfs_mounts.inspect(home_ds)
    if st.mounted and os.path.abspath(st.where or "") == mp:
        return True
    zfs_mounts.ensure_mounted(home_ds, target=mp, log=log)
    st = zfs_mounts.inspect(home_ds)
    return bool(st.mounted and os.path.abspath(st.where or "") == mp)


def ensure_user(user, groups):
    """Cree l'utilisateur dedie (non-root) s'il manque et l'ajoute aux groupes
    requis (video/input/seat/render : acces GPU/entrees via seatd). Retourne
    (uid, gid, home) ou None si echec. Idempotent."""
    import pwd
    import grp
    try:
        pw = pwd.getpwnam(user)
        # s'assurer des groupes meme si l'utilisateur existe deja
        for g in groups:
            try:
                grp.getgrnam(g)
                subprocess.run(["gpasswd", "-a", user, g],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except KeyError:
                pass                       # groupe absent : on ignore (pas bloquant)
        return pw.pw_uid, pw.pw_gid, pw.pw_dir
    except KeyError:
        pass
    # creation : home dans /home/<user>, shell bash, groupes
    if not which("useradd"):
        log(f"[session] useradd absent : impossible de creer l'utilisateur {user}")
        return None
    # GARDE-FOU : /home DOIT etre data_pool/home monte avant --create-home, sinon
    # le home atterrit sur l'overlay racine (volatil, mauvais dataset). On le
    # monte ; si on n'y arrive pas, on REFUSE plutot que de polluer la racine.
    if not _home_dataset_ready():
        log("[session] data_pool/home NON monte sur /home -> creation de "
            f"{user} REFUSEE (eviterait un home sur la racine/overlay). "
            "Verifier le montage ZFS (zfs_mounts/ensure_zfs_booted).")
        return None
    existing = []
    for g in groups:
        try:
            grp.getgrnam(g)
            existing.append(g)
        except KeyError:
            log(f"[session] groupe '{g}' absent (ignore)")
    cmd = ["useradd", "--create-home", "--shell", "/bin/bash"]
    if existing:
        cmd += ["--groups", ",".join(existing)]
    cmd.append(user)
    if subprocess.run(cmd).returncode != 0:
        log(f"[session] echec creation utilisateur {user}")
        return None
    try:
        pw = pwd.getpwnam(user)
        log(f"[session] utilisateur {user} cree (uid={pw.pw_uid}, "
            f"groupes={','.join(existing)})")
        return pw.pw_uid, pw.pw_gid, pw.pw_dir
    except KeyError:
        return None


def _fast_tmp_src():
    """Mountpoint de fast_pool/tmp (tmp commun volatil). Le monte au besoin.
    Retourne le chemin ou '' si indisponible."""
    try:
        mp = subprocess.run(["zfs", "get", "-H", "-o", "value", "mountpoint",
                             "fast_pool/tmp"], capture_output=True,
                            text=True).stdout.strip()
    except OSError:
        return ""
    if not mp.startswith("/") or mp in ("none", "legacy"):
        return ""
    if not os.path.ismount(mp):
        subprocess.run(["zfs", "mount", "fast_pool/tmp"],
                       stderr=subprocess.DEVNULL)
    return mp if os.path.isdir(mp) else ""


def setup_user_dirs(uid, gid, home, infra_path="/etc/infra.conf"):
    """Prepare les donnees de l'utilisateur de session :
      - ~/.autoboot/{rag,brainstorm} : DURABLE (data_pool/home) ;
      - ~/fast : tmp commun VOLATIL (bind sur fast_pool/tmp) ;
      - exporte AUTOBOOT_HOME + MODELS_DIR pour les enfants (app, rag,
        brainstorm, operate). Best-effort : aucune erreur ne bloque la session."""
    for sub in (".autoboot", ".autoboot/rag", ".autoboot/brainstorm"):
        p = os.path.join(home, sub)
        try:
            os.makedirs(p, exist_ok=True)
            os.chown(p, uid, gid)
        except OSError as e:
            log(f"[session] {p} non prepare ({e})")
    fast = os.path.join(home, "fast")
    try:
        os.makedirs(fast, exist_ok=True)
        os.chown(fast, uid, gid)
    except OSError:
        pass
    src = _fast_tmp_src()
    if src and not os.path.ismount(fast):
        rc = subprocess.run(["mount", "--bind", src, fast]).returncode
        log(f"[session] ~/fast -> {src} " + ("OK" if rc == 0 else "(bind echoue)"))
    elif not src:
        log("[session] fast_pool/tmp non monte -> ~/fast indisponible")
    os.environ["AUTOBOOT_HOME"] = home
    try:
        from configobj import ConfigObj
        md = (ConfigObj(infra_path).get("inference", {}) or {}).get("models_dir")
        if md:
            os.environ.setdefault("MODELS_DIR", md)
    except Exception:
        pass


def _demote(uid, gid):
    """Retourne une fonction preexec qui bascule le processus enfant vers
    l'utilisateur non-root (setgid puis setuid, ordre important)."""
    def preexec():
        os.setgid(gid)
        os.setuid(uid)
    return preexec


def run_session_app(scfg, uid, gid, home):
    """Lance cage avec l'app principale, EN TANT QUE l'utilisateur dedie (non-root).
    Retourne le code retour de cage. Le verrou swaylock (si active) est gere a
    part. PID 1 (root) reste le parent : il survit a la sortie de cage."""
    app = scfg["app"]
    app_argv = app.split() if isinstance(app, str) else list(app)
    # XDG_RUNTIME_DIR de l'utilisateur (pas /run/user/0)
    user_runtime = f"/run/user/{uid}"
    os.makedirs(user_runtime, exist_ok=True)
    os.chown(user_runtime, uid, gid)
    os.chmod(user_runtime, 0o700)
    env = dict(os.environ,
               HOME=home, USER=scfg["user"], LOGNAME=scfg["user"],
               XDG_RUNTIME_DIR=user_runtime)
    if not which("cage"):
        log("[session] cage absent")
        return 127
    log(f"[session] cage -- {app} (utilisateur {scfg['user']}, uid={uid})")
    try:
        return subprocess.run(["cage", "--"] + app_argv,
                              env=env, preexec_fn=_demote(uid, gid)).returncode
    except OSError as e:
        log(f"[session] echec lancement cage en non-root : {e}")
        return 1


def start_idle_lock(scfg, uid, gid, home):
    """Verrou d'ecran sur inactivite via swayidle + swaylock (si lock.enabled).
    swaylock valide le mot de passe du compte via PAM. Lance en tant que
    l'utilisateur. Best effort (non bloquant). Une couche python-pam peut etre
    ajoutee ici pour une logique d'auth personnalisee."""
    if not scfg["lock_enabled"]:
        return
    if scfg["lock_backend"] != "swaylock" or not which("swaylock"):
        log("[session] verrou demande mais swaylock absent -> pas de verrou. "
            "(emerge gui-apps/swaylock)")
        return
    user_runtime = f"/run/user/{uid}"
    env = dict(os.environ, HOME=home, USER=scfg["user"],
               XDG_RUNTIME_DIR=user_runtime)
    lock_cmd = ["swaylock"] + scfg["lock_options"]
    idle = scfg["lock_idle"]
    if idle > 0 and which("swayidle"):
        # verrouille apres <idle> s d'inactivite
        subprocess.Popen(
            ["swayidle", "-w", "timeout", str(idle), " ".join(lock_cmd)],
            env=env, preexec_fn=_demote(uid, gid),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"[session] verrou auto apres {idle}s d'inactivite (swayidle+swaylock)")
    else:
        log("[session] verrou swaylock disponible (swayidle absent ou idle=0 : "
            "pas de verrouillage automatique)")


def main():
    os.environ.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    for src, tgt, fs in (("proc", "/proc", "proc"), ("sysfs", "/sys", "sysfs"),
                         ("devtmpfs", "/dev", "devtmpfs"), ("tmpfs", "/run", "tmpfs")):
        mount(src, tgt, fs)
    os.makedirs("/dev/pts", exist_ok=True)
    subprocess.run(["mount", "-t", "devpts", "devpts", "/dev/pts"],
                   stderr=subprocess.DEVNULL)
    # COMPLETER /dev : liens standards + eudev (cree /dev/dri, /dev/fd...).
    # Sans ca : emerge 'sane /dev' KO, bash process-substitution KO, cage KO.
    setup_dev()
    # ENVIRONNEMENT : base systeme (HOME/USER/PATH/LANG) + variables Wayland,
    # pour PID 1 et tous ses enfants (seatd, cage, XWayland a venir).
    setup_environment()

    # MODE DEGRADE : rapport + shell de reparation, pas de session normale
    if os.path.exists("/etc/rescue-mode"):
        log("mode degrade detecte -> reparation (session normale annulee)")
        degraded_repair()                  # ne revient pas

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    os.environ["XDG_RUNTIME_DIR"] = RUNTIME_DIR

    # BRING-UP OpenRC AVANT tout 'rc-service' : initialise le runtime
    # (/run/openrc : deptree, verrous, softlevel) et demarre sysfs/devfs/udev
    # comme services OpenRC. C'est ce qui corrige les echecs 'bad file
    # descriptor', 'sysfs would not start', 'devfs failed' constates au demarrage
    # des services. udev de ce runlevel peuple /dev/dri (perms GPU) avant seatd/cage.
    openrc_bringup()

    # REMONTER les datasets ZFS du systeme booted : les montages /mnt/* de
    # l'initramfs ont disparu au switch_root (pools toujours importes). Sans ca :
    # fast_pool/sfs, staging, boot_pool/manager, images... absents en booted.
    ensure_zfs_booted()

    # token GitHub (boot_pool/manager dispo apres ensure_zfs_booted) -> reactive
    # le reporting git (push board de first_boot, synchro manager d'operate/
    # boot_confirm). Sans lui, tout push est silencieusement saute.
    load_github_token()

    if which("seatd"):
        subprocess.Popen(["seatd", "-g", "video"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ["LIBSEAT_BACKEND"] = "seatd"
        time.sleep(1)
        log("seatd demarre")

    # SOCLE OpenRC (syslog-ng, dbus, udev...) dans l'ordre de [services] de
    # infra.conf. Lance AVANT le stream pour que les logs (syslog-ng -> /var/log)
    # et le bus (dbus) soient prets, et que le boot 'complet' soit streame ensuite.
    # socle log : preparer la socket syslog-ng (repertoire logs:logs + lien
    # /dev/log) AVANT start_services, sinon syslog-ng en 'logs' ne peut pas
    # creer /dev/log (sans capabilities).
    prepare_syslog_socket()

    start_services()

    # PROMOTION du noyau fraichement boote : health-check -> efibootmgr + registre
    # -> remontee git. En arriere-plan (ne bloque pas le compositeur). Le reseau
    # (initramfs) et le token sont prets a ce stade.
    run_boot_confirm()

    # SESSION utilisateur dediee (non-root) : lire [session], creer l'utilisateur
    # + groupes (video/input/seat/render), preparer le verrou. cage tournera en
    # tant que cet utilisateur ; PID 1 (root) reste le parent qui survit.
    scfg = _read_session_config()
    session_user = ensure_user(scfg["user"], scfg["groups"])
    if session_user:
        setup_user_dirs(*session_user)

    key = read_key()
    stop_initramfs_stream()

    # capture wayland en arriere-plan (attend le compositeur). Demarre APRES le
    # socle -> reseau (initramfs) + heure + logs prets quand le stream commence.
    if os.fork() == 0:
        start_wayland_stream(key)
        os._exit(0)

    # Compositeur kiosk EN UTILISATEUR DEDIE. CRITIQUE : jamais d'execvp direct
    # (remplacerait PID 1 -> si cage meurt, kernel panic). On lance en
    # SOUS-PROCESSUS demote (setuid/setgid vers l'utilisateur), on surveille,
    # et en cas d'echec on retombe sur un shell. PID 1 (root) ne quitte JAMAIS.
    def run_compositor():
        if session_user:
            uid, gid, home = session_user
            # verrou d'inactivite (swaylock) si active, lance avec la session
            start_idle_lock(scfg, uid, gid, home)
            rc = run_session_app(scfg, uid, gid, home)
            if rc != 0 and scfg["app"] != scfg["app_fallback"]:
                log(f"[session] app '{scfg['app']}' echouee -> repli "
                    f"'{scfg['app_fallback']}'")
                scfg2 = dict(scfg, app=scfg["app_fallback"])
                rc = run_session_app(scfg2, uid, gid, home)
            return rc
        # repli : pas d'utilisateur dedie (creation echouee) -> cage en root
        log("[session] pas d'utilisateur dedie -> cage en root (repli)")
        if which("cage"):
            return subprocess.run(["cage", "--", scfg["app_fallback"]]).returncode
        if which("sway"):
            return subprocess.run(["sway"]).returncode
        log("aucun compositeur (cage/sway) installe")
        return 127

    # BOUCLE PID 1 : le compositeur peut se TERMINER (tu fermes foot) ou ECHOUER.
    # Dans les DEUX cas, PID 1 ne doit pas mourir (sinon kernel panic). On
    # relance le compositeur ; s'il echoue a repetition (backend KO), on bascule
    # sur un shell de maintenance (puis on retente le compositeur apres le shell).
    consecutive_fail = 0
    while True:
        rc = run_compositor()
        if rc == 0:
            consecutive_fail = 0
            log("compositeur termine proprement -> relance "
                "(PID 1 doit rester vivant). Ferme via 'poweroff' pour eteindre.")
            time.sleep(1)
            continue
        # echec
        consecutive_fail += 1
        has_dri = os.path.exists("/dev/dri/card0")
        log("=" * 56)
        log(f"COMPOSITEUR ECHEC (rc={rc}, echec #{consecutive_fail}).")
        log(f"  /dev/dri/card0 present : {has_dri}")
        if not has_dri:
            log("  -> AUCUN device DRM (nomodeset ? eudev ? module i915/xe ?).")
        if consecutive_fail >= 3:
            log("  3 echecs consecutifs -> SHELL de maintenance "
                "(PID 1 reste vivant). 'exit' pour retenter le compositeur.")
            log("=" * 56)
            subprocess.run(["bash", "-l"] if os.path.exists("/bin/bash")
                           else ["sh", "-l"])
            consecutive_fail = 0           # apres le shell, on retente
        else:
            log("  -> nouvelle tentative dans 2 s...")
            log("=" * 56)
            time.sleep(2)


if __name__ == "__main__":
    main()

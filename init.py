#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
/init — PID 1 de l'initramfs, 100% Python (ctypes).

Chemin critique sans shell : mount(2), finit_module(2), loop via ioctl,
switch_root reimplemente (MS_MOVE + chroot + execv). busybox n'est present
que comme shell de secours si quelque chose echoue.

Deploye comme /init (shebang). Requiert CONFIG_BINFMT_SCRIPT=y et CPython
embarque dans l'initramfs (cf. build_initramfs.py).
"""
import ctypes
import ctypes.util
import fcntl
import os
import shutil
import subprocess
import sys
import time

# --- parametres --------------------------------------------------------------
KVER        = os.uname().release
POOL        = "fast_pool"
SFS_DS      = f"{POOL}/sfs"
ROOTFS_SFS  = "rootfs.sfs"
MODULES_SFS = f"modules-{KVER}.sfs"
NEWROOT     = "/mnt/root"

# Import RAPIDE : -d cible sur les seuls devices des pools (evite de scanner tout
# /dev y compris l'ISO live -> import qui passe de ~37s a <1s). Les chemins sont
# ceux des partitions ZFS reelles (cf. blkid). On limite le scan a /dev/disk/by-id
# si dispo (stable), sinon aux partitions listees. NB: -d peut etre repete.
IMPORT_SCAN_DIRS = ["/dev"]          # rempli plus finement par _scan_dirs()
DATA_POOL   = "data_pool"            # importe aussi (donnees : home, log, archives)

# --- mode secours (degrade niveau 1) -----------------------------------------
BOOT_POOL      = "boot_pool"          # manager (manifest/journal) + images secours
RESCUE_POOL    = BOOT_POOL
RESCUE_SFS_DS  = f"{RESCUE_POOL}/images"
RESCUE_ROOTFS  = "rootfs.sfs"

# --- couches persistantes (datasets fast_pool montes au boot) ----------------
# upper de l'overlay racine + montages directs. En mode SECOURS (fast_pool
# absent), aucun n'est disponible -> on retombe sur un upper tmpfs volatile.
UPPER_DS   = f"{POOL}/rootfs"     # upper de l'overlay racine (systeme mutable)
LOG_DS     = f"{POOL}/log"        # monte sur NEWROOT/var/log (persistant ; /var lui-meme reste dans l'overlay rootfs)
USRSRC_DS  = f"{POOL}/usr-src"    # monte sur NEWROOT/usr/src (sources noyau, build)

IP_ADDR  = "192.168.1.10/24"   # redondant si ip= passe par la cmdline noyau
GATEWAY  = "192.168.1.1"
DNS      = "8.8.8.8"

YT_KEY_FILE = "/etc/yt.key"    # cle deposee dans l'initramfs (cf. build_initramfs)
RTMP   = "rtmp://a.rtmp.youtube.com/live2"
FB     = "/dev/fb0"
FPS, VBR = "30", "4500k"


def read_yt_key():
    """Cle de stream YouTube embarquee dans l'initramfs (/etc/yt.key).
    Absente -> pas de stream initramfs (non bloquant)."""
    try:
        with open(YT_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""

# --- constantes noyau (x86_64) ----------------------------------------------
MS_RDONLY = 1
MS_BIND   = 4096
MS_MOVE   = 8192
LOOP_CTL_GET_FREE = 0x4C82
LOOP_SET_FD       = 0x4C00
NR_finit_module   = 313        # x86_64
EEXIST = 17


def _load_libc():
    """Charge la libc via ctypes. find_library('c') depend de gcc/ldconfig/
    objdump, ABSENTS de l'initramfs -> on essaie des chemins explicites. Gentoo
    met la libc dans /usr/lib64 ; on couvre les emplacements usuels."""
    candidates = []
    try:
        f = ctypes.util.find_library("c")
        if f:
            candidates.append(f)
    except Exception:
        pass
    candidates += ["libc.so.6",
                   "/usr/lib64/libc.so.6", "/lib64/libc.so.6",
                   "/usr/lib/libc.so.6", "/lib/libc.so.6",
                   "/usr/lib/x86_64-linux-gnu/libc.so.6"]
    last = None
    for c in candidates:
        try:
            return ctypes.CDLL(c, use_errno=True)
        except OSError as e:
            last = e
    # echec total : message sur kmsg/console AVANT de mourir (sinon crash muet)
    msg = f"FATAL: libc introuvable ({last}). Essais: {candidates}"
    for dev in ("/dev/kmsg", "/dev/console"):
        try:
            with open(dev, "w") as fh:
                fh.write("[init.py] " + msg + "\n")
        except OSError:
            pass
    raise SystemExit(msg)


libc = _load_libc()
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                       ctypes.c_ulong, ctypes.c_char_p]
libc.mount.restype = ctypes.c_int
libc.syscall.restype = ctypes.c_long


def log(msg):
    line = f"[init] {msg}\n"
    try:
        with open("/dev/kmsg", "w") as k:
            k.write(line)
    except OSError:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except OSError:
        pass


def die(msg):
    log(f"ERREUR: {msg}")
    log("shell de secours.")
    for sh in ("/bin/sh", "/bin/busybox"):
        if os.path.exists(sh):
            try:
                os.execv(sh, [sh] if sh.endswith("sh") else [sh, "sh"])
            except OSError:
                pass
    try:                                   # repli : REPL python
        import code
        code.interact(banner="initramfs rescue (python)", local=globals())
    except Exception:
        pass
    os._exit(1)


# --- debug pilote par /proc/cmdline ---------------------------------------- #
# 'debug'         -> logs verbeux (debug_log visible) + ne pas rebooter au panic
# 'break=<etape>' -> ouvre un shell AVANT l'etape nommee (pseudofs|zfs|overlay|
#                    persist|switch). Permet d'inspecter l'etat a la main.
_CMDLINE = None


def cmdline():
    """Contenu de /proc/cmdline (cache). '' si illisible."""
    global _CMDLINE
    if _CMDLINE is None:
        try:
            with open("/proc/cmdline") as f:
                _CMDLINE = f.read().strip()
        except OSError:
            _CMDLINE = ""
    return _CMDLINE


def debug_enabled():
    toks = cmdline().split()
    return "debug" in toks or "init_debug" in toks


def debug_log(msg):
    """Message visible uniquement si 'debug' dans la cmdline."""
    if debug_enabled():
        log(f"DEBUG {msg}")


def break_requested(stage):
    """L'utilisateur a-t-il demande 'break=<stage>' (ou 'break' seul = a la fin) ?"""
    for tok in cmdline().split():
        if tok == f"break={stage}":
            return True
    return False


def debug_shell(stage):
    """Ouvre un shell interactif AVANT l'etape `stage` si demande. Le boot
    reprend a la sortie du shell (exit). Ne tue PAS le boot (contrairement a
    die)."""
    if not break_requested(stage):
        return
    log(f"=== BREAK avant '{stage}' : shell de debug (exit pour continuer) ===")
    log(f"    cmdline: {cmdline()}")
    for sh in ("/bin/sh", "/bin/busybox"):
        if os.path.exists(sh):
            try:
                # subprocess : on REVIENT apres l'exit du shell (pas execv)
                subprocess.run([sh] if sh.endswith("sh") else [sh, "sh"])
                log(f"=== reprise apres '{stage}' ===")
                return
            except OSError:
                pass
    log("    (aucun shell disponible)")


def mount(src, tgt, fstype, flags=0, data=None):
    s = src.encode() if src else None
    t = tgt.encode()
    f = fstype.encode() if fstype else None
    d = data.encode() if data else None
    if libc.mount(s, t, f, ctypes.c_ulong(flags), d) != 0:
        e = ctypes.get_errno()
        # EBUSY (16) = deja monte (le lanceur /init monte proc/sys/dev tot pour
        # le debug). Ce n'est pas une erreur : la cible est disponible.
        if e == 16:
            return
        raise OSError(e, f"mount({src} -> {tgt}, {fstype}): {os.strerror(e)}")


def load_module(path):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        r = libc.syscall(NR_finit_module, ctypes.c_int(fd),
                         ctypes.c_char_p(b""), ctypes.c_int(0))
        if r != 0:
            e = ctypes.get_errno()
            if e != EEXIST:               # deja charge = ok
                raise OSError(e, f"finit_module({path}): {os.strerror(e)}")
    finally:
        os.close(fd)


def losetup(backing, readonly=True):
    cfd = os.open("/dev/loop-control", os.O_RDWR)
    try:
        num = fcntl.ioctl(cfd, LOOP_CTL_GET_FREE)
    finally:
        os.close(cfd)
    dev = f"/dev/loop{num}"
    bfd = os.open(backing, os.O_RDONLY if readonly else os.O_RDWR)
    lfd = os.open(dev, os.O_RDWR)          # le device loop doit etre RW pour LOOP_SET_FD
    try:
        fcntl.ioctl(lfd, LOOP_SET_FD, bfd)
    finally:
        os.close(lfd)
        os.close(bfd)
    return dev


def run(cmd):
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if p.stdout:
        try:
            sys.stdout.write(p.stdout.decode(errors="replace"))
            sys.stdout.flush()
        except OSError:
            pass
    return p.returncode


def capture(cmd):
    """Comme run() mais retourne (rc, texte) sans echo ; pour parser une sortie."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode(errors="replace")
    except (OSError, ValueError) as e:
        return 1, str(e)



def zfs_mountpoint(dataset):
    """Valeur de la propriete mountpoint du dataset ('legacy', '/chemin', ou '')."""
    rc, out = capture(["zfs", "get", "-H", "-o", "value", "mountpoint", dataset])
    return out.strip() if rc == 0 else ""


def ds_mounted(dataset):
    """Le dataset est-il REELLEMENT monte ? Verite terrain : /proc/mounts ou la
    propriete ZFS 'mounted'. On EVITE os.path.ismount (peu fiable sur ZFS, ex en
    chroot : st_dev trompeur). init.py reste autonome : lecture directe."""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and p[2] == "zfs" and p[0] == dataset:
                    return True
    except OSError:
        pass
    rc, out = capture(["zfs", "get", "-H", "-o", "value", "mounted", dataset])
    return rc == 0 and out.strip() == "yes"


def remount_to(dataset, target, allow_nonempty=False):
    """Rend le contenu de `dataset` disponible a `target` (sous NEWROOT), SANS
    toucher a la propriete mountpoint. Approche EPUREE (une seule convention) :
      1. s'assurer que le dataset est monte a son emplacement naturel (zfs mount,
         ou mount.zfs si legacy) ;
      2. bind de cet emplacement vers `target`.
    GARDE MASQUAGE : refuse si target non-vide, sauf allow_nonempty (ex /var/log).
    Retourne True/False. Ne parie sur aucun automount : monte explicitement."""
    # garde masquage
    if not allow_nonempty:
        try:
            if os.path.isdir(target) and os.listdir(target):
                log(f"  [!] {dataset} : {target} NON-VIDE -> refus "
                    f"(masquerait des fichiers)")
                return False
        except OSError:
            pass
    mp = zfs_mountpoint(dataset)
    is_legacy = (mp == "legacy")
    os.makedirs(target, exist_ok=True)

    if is_legacy:
        # legacy : monter directement a la cible
        if run(["mount.zfs", dataset, target]) == 0:
            log(f"  {dataset} (legacy) -> {target}")
            return True
        log(f"  [!] {dataset} (legacy) : mount.zfs echoue")
        return False

    # non-legacy : s'assurer qu'il est monte a son emplacement naturel
    if not ds_mounted(dataset):
        subprocess.run(["zfs", "mount", dataset], stderr=subprocess.DEVNULL)
    if not ds_mounted(dataset):
        if mp and mp != "legacy":
            os.makedirs(mp, exist_ok=True)
            run(["mount.zfs", dataset, mp])
    if not ds_mounted(dataset):
        log(f"  [!] {dataset} : impossible a monter")
        return False
    # bind de l'emplacement naturel vers la cible finale
    if os.path.abspath(mp) == os.path.abspath(target):
        log(f"  {dataset} -> {target} (deja en place)")
        return True
    if run(["mount", "--bind", mp, target]) == 0:
        log(f"  {dataset} : {mp} --bind-> {target}")
        return True
    log(f"  [!] bind {mp} -> {target} echoue")
    return False


def _scan_dirs():
    """Repertoires/devices a passer en -d a zpool import : on CIBLE les devices
    des pools au lieu de scanner tout /dev (qui inclut l'ISO live -> ~37s/pool).
    Prefere /dev/disk/by-id (stable). Retourne une liste d'arguments -d."""
    candidates = ["/dev/disk/by-id", "/dev/disk/by-partuuid"]
    dirs = [d for d in candidates if os.path.isdir(d) and os.listdir(d)]
    if dirs:
        args = []
        for d in dirs:
            args += ["-d", d]
        return args
    return ["-d", "/dev"]              # repli : au moins /dev (mieux que rien)


def import_pool(pool, mount=False):
    """Importe `pool` RAPIDEMENT. Strategie :
      1. cachefile /etc/zfs/zpool.cache s'il existe (instantane, pas de scan) ;
      2. sinon -d cible (by-id/by-partuuid) au lieu de tout /dev.
    -N = ne pas monter (on monte ensuite dans l'ordre). Retourne 0 si OK."""
    if os.path.exists("/etc/zfs/zpool.cache"):
        rc = run(["zpool", "import", "-c", "/etc/zfs/zpool.cache",
                  "-N", "-f", pool])
        if rc == 0:
            return 0
        log(f"  cachefile inutilisable pour {pool}, scan cible...")
    flag = [] if mount else ["-N"]
    return run(["zpool", "import"] + _scan_dirs() + flag + ["-f", pool])


def mount_zfs_dataset(dataset, target):
    """Monte un dataset a `target`, que son mountpoint soit 'legacy' OU un chemin.
    mount.zfs gere les deux si on lui donne la cible explicite. Retourne True/False.
    (Corrige le bug : 'mount.zfs fast_pool/sfs /mnt/sfs' echouait quand le
    mountpoint != legacy ; on force via -o zfsutil pour les non-legacy.)"""
    os.makedirs(target, exist_ok=True)
    mp = zfs_mountpoint(dataset)
    if mp == "legacy":
        return run(["mount.zfs", dataset, target]) == 0
    # non-legacy : mount.zfs accepte -o zfsutil pour monter a une cible arbitraire
    if run(["mount.zfs", "-o", "zfsutil", dataset, target]) == 0:
        return True
    # repli : laisser zfs monter a son mountpoint naturel puis bind
    return remount_to(dataset, target, allow_nonempty=True)


def list_pool_datasets(pool):
    """Datasets du pool TRIES par profondeur (parent avant enfant). Indispensable :
    boot_pool doit etre monte avant boot_pool/images, etc."""
    rc, out = capture(["zfs", "list", "-H", "-o", "name", "-r", pool])
    if rc != 0:
        return []
    names = [n for n in out.splitlines() if n.strip()]
    # tri par nombre de '/' (profondeur) puis alphabetique -> parent en premier
    names.sort(key=lambda n: (n.count("/"), n))
    return names


def mount_pool_recursive(pool, under, respect_mountpoint=False):
    """Monte TOUS les datasets montables d'un pool SOUS `under`, dans l'ordre
    parent->enfant. Un dataset dont le PARENT a echoue est saute (dependance).
    mountpoint=none/legacy traites correctement. Retourne (ok:set, failed:set).

    respect_mountpoint=True : la cible est NEWROOT + mountpoint_systeme du dataset
    (ex data_pool/home mountpoint=/home -> NEWROOT/home), au lieu d'empiler sous
    `under`. Indispensable pour que /home et /modeles atterrissent au bon endroit
    (sinon data_pool/home finit a NEWROOT/mnt/data/home = sur l'upper volatil)."""
    ok, failed = set(), set()
    for ds in list_pool_datasets(pool):
        parent = ds.rsplit("/", 1)[0] if "/" in ds else None
        if parent and parent in failed:
            log(f"  [skip] {ds} : parent {parent} non monte (dependance)")
            failed.add(ds)
            continue
        mp = zfs_mountpoint(ds)
        if mp == "none":
            ok.add(ds)                # 'none' = conteneur, rien a monter, OK
            continue
        if (respect_mountpoint and mp not in ("legacy", "none", "-", "")
                and mp.startswith("/")):
            # respecter le mountpoint systeme : NEWROOT + /home -> NEWROOT/home
            target = under.rstrip("/") + mp
        else:
            # cible sous `under` : on reproduit l'arborescence relative au pool
            rel = ds[len(pool):].lstrip("/")
            target = os.path.join(under, rel) if rel else under
        if mount_zfs_dataset(ds, target):
            ok.add(ds)
            log(f"  monte {ds} -> {target}")
        else:
            failed.add(ds)
            log(f"  [!] {ds} NON monte")
    return ok, failed


def disk_inventory():
    """Liste les disques physiques vus par le noyau (hors loop/ram/zram)."""
    disks = []
    try:
        for name in sorted(os.listdir("/sys/block")):
            if name.startswith(("loop", "ram", "zram", "dm-")):
                continue
            size_p = f"/sys/block/{name}/size"
            try:
                with open(size_p) as f:
                    sectors = int(f.read().strip())
                gb = sectors * 512 / 1e9
            except (OSError, ValueError):
                gb = 0.0
            disks.append((name, round(gb, 1)))
    except OSError as e:
        log(f"inventaire disques indisponible ({e})")
    return disks


def memory_check():
    """Check memoire LEGER (pas un memtest) : erreurs ECC/EDAC + dmesg.
    Retourne (ok, [messages])."""
    msgs = []
    ok = True
    edac = "/sys/devices/system/edac/mc"
    try:
        if os.path.isdir(edac):
            for mc in sorted(os.listdir(edac)):
                for kind in ("ce_count", "ue_count"):   # corrigees / non-corrigees
                    p = f"{edac}/{mc}/{kind}"
                    try:
                        with open(p) as f:
                            n = int(f.read().strip())
                    except (OSError, ValueError):
                        continue
                    if n > 0:
                        msgs.append(f"EDAC {mc}/{kind}={n}")
                        if kind == "ue_count":
                            ok = False               # erreur non-corrigee = serieux
        else:
            msgs.append("EDAC absent (ECC non expose ou non-ECC)")
    except OSError as e:
        msgs.append(f"EDAC illisible ({e})")
    rc, out = capture(["dmesg"])
    if rc == 0:
        for line in out.splitlines():
            low = line.lower()
            if "memory error" in low or "hardware error" in low or "mce:" in low:
                msgs.append("dmesg: " + line.strip()[:120])
                ok = False
    return ok, msgs


def zpool_health(pool):
    """Retourne (state, txt). state in {ONLINE,DEGRADED,FAULTED,UNKNOWN}."""
    rc, out = capture(["zpool", "status", "-x", pool])
    rc2, st = capture(["zpool", "list", "-H", "-o", "health", pool])
    state = st.strip().upper() if rc2 == 0 and st.strip() else "UNKNOWN"
    return state, out


def health_check():
    """Etape 2bis : ZFS + disques + memoire. BLOQUE seulement si pool FAULTED
    (DEGRADED -> on logue et on continue, le stream reste prioritaire).
    Ecrit /run/health.json pour l'inference (etape 4 post-boot)."""
    report = {"pool": None, "pool_state": None, "disks": [], "memory_ok": True,
              "memory_msgs": [], "blocked": False}

    disks = disk_inventory()
    report["disks"] = [{"name": n, "size_gb": g} for n, g in disks]
    log(f"disques: {', '.join(f'{n}({g}G)' for n, g in disks) or 'aucun'}")

    state, txt = zpool_health(POOL)
    report["pool"] = POOL
    report["pool_state"] = state
    log(f"etat pool {POOL}: {state}")
    if state in ("FAULTED", "UNAVAIL"):
        report["blocked"] = True
        _write_health(report)
        die(f"pool {POOL} {state} -- boot bloque (donnees a risque)\n{txt}")
    elif state in ("DEGRADED", "UNKNOWN"):
        log(f"AVERTISSEMENT pool {state} -- on continue\n{txt}")

    mem_ok, mem_msgs = memory_check()
    report["memory_ok"] = mem_ok
    report["memory_msgs"] = mem_msgs
    for m in mem_msgs:
        log(f"memoire: {m}")
    if not mem_ok:
        log("AVERTISSEMENT memoire (erreurs non-corrigees) -- on continue, "
            "envisager un memtest86+ au prochain reboot")

    _write_health(report)
    return report


def _write_health(report):
    import json
    try:
        os.makedirs("/run", exist_ok=True)
        with open("/run/health.json", "w") as f:
            json.dump(report, f)
    except OSError as e:
        log(f"/run/health.json non ecrit ({e})")


def wait_fb(timeout=8.0):
    """Attend l'apparition de /dev/fb0 (simpledrm/efifb tres tot, xe ensuite).
    Retourne True si present. Non bloquant au-dela du timeout."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(FB):
            return True
        time.sleep(0.2)
    return os.path.exists(FB)


def start_boot_stream(key):
    """Demarre le stream de la CONSOLE DE BOOT des l'initramfs (on voit les
    logs en direct sur YouTube). ffmpeg statique doit etre embarque. Le pid est
    transmis a session_launch via /run (puis recopie sous NEWROOT) pour la
    bascule vers la capture wayland apres switch_root.
    Retourne le Popen, ou None si indisponible (jamais bloquant)."""
    ff = "/usr/bin/ffmpeg"
    if not key:
        log("pas de cle YouTube (/etc/yt.key) -> pas de stream initramfs")
        return None
    if not os.path.exists(ff):
        log("ffmpeg absent de l'initramfs -> pas de stream initramfs "
            "(FFMPEG_STATIC au build)")
        return None
    if not wait_fb():
        log(f"{FB} indisponible -> stream initramfs differe (session_launch)")
        return None
    cmd = [ff, "-nostdin", "-loglevel", "error",
           "-f", "fbdev", "-framerate", FPS, "-i", FB,
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
           "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
           "-b:v", VBR, "-maxrate", VBR, "-bufsize", "9000k",
           "-pix_fmt", "yuv420p", "-g", "60",
           "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
           "-f", "flv", f"{RTMP}/{key}"]
    try:
        logf = open("/run/boot-stream.log", "wb")
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
    except OSError as e:
        log(f"stream initramfs non demarre ({e})")
        return None
    try:                                   # handoff pour session_launch
        with open("/run/initramfs-stream.pid", "w") as f:
            f.write(f"{proc.pid}\n")
        with open("/run/yt.key", "w") as f:
            f.write(f"{key}\n")
    except OSError:
        pass
    log(f"STREAM console de boot demarre (pid {proc.pid}) -> YouTube")
    return proc


def ds_exists(dataset):
    """Le dataset ZFS existe-t-il ? (distingue 'absent' de 'corrompu')."""
    rc, _ = capture(["zfs", "list", "-H", "-o", "name", dataset])
    return rc == 0


def pool_imported(pool):
    """Le pool est-il importe (visible par zpool list) ?"""
    rc, _ = capture(["zpool", "list", "-H", "-o", "name", pool])
    return rc == 0


def writable_test(path):
    """Verifie qu'on peut reellement ecrire sous path (detecte un FS monte
    mais corrompu/lecture seule). Retourne True si OK."""
    probe = os.path.join(path, ".init_write_probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        return True
    except OSError:
        return False


def sfs_crc32(path):
    """CRC32 (zlib, standard 'crc32-b') du rootfs.sfs, en streaming par blocs
    (pas de chargement complet). Sert a detecter un CHANGEMENT de sfs sous un
    upper persistant. Rapide ; pas crypto, juste de l'identite. Retourne une
    chaine hex, ou '' si illisible."""
    import zlib
    crc = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
        return f"{crc & 0xffffffff:08x}"
    except OSError:
        return ""


def upper_stale(upper_mnt, sfs_crc):
    """L'upper persistant a-t-il ete engendre par un AUTRE sfs ? Compare le
    marqueur .sfs-crc (ecrit dans l'upper) au CRC du sfs courant.
    Retourne (stale, ancien_crc). stale=False si marqueur absent (1er boot)."""
    marker = os.path.join(upper_mnt, "upper", ".sfs-crc")
    try:
        with open(marker) as f:
            old = f.read().strip()
    except OSError:
        return (False, "")               # pas de marqueur -> 1er usage, pas perime
    return (old != sfs_crc, old)


def write_sfs_marker(upper_mnt, sfs_crc):
    try:
        with open(os.path.join(upper_mnt, "upper", ".sfs-crc"), "w") as f:
            f.write(sfs_crc + "\n")
    except OSError as e:
        log(f"marqueur .sfs-crc non ecrit ({e})")


def _strip_overlay_xattrs(path):
    """Retire les xattr 'trusted.overlay.*' d'un repertoire (typiquement la
    racine de l'upperdir). Ces xattr (origin/impure/opaque/redirect) referencent
    l'ANCIEN lower ; les laisser sur un upper reutilise avec un NOUVEAU sfs ->
    'failed to verify upper root origin' / Stale file handle (ESTALE). Best
    effort, sans dependance externe (os.removexattr)."""
    try:
        names = os.listxattr(path)
    except OSError:
        return
    for name in names:
        if name.startswith("trusted.overlay."):
            try:
                os.removexattr(path, name)
            except OSError:
                pass


def snapshot_and_reset_upper(old_crc, sfs_crc):
    """Le sfs a change : on SNAPSHOTE l'ancien upper (coherent), on l'ENVOIE
    vers data_pool/archives (durable), puis on VIDE le contenu de l'upper (PAS
    le dataset -> on garde ses proprietes xattr/acl). Retourne un message de
    bilan (non bloquant : un echec de snapshot ne doit pas empecher de booter,
    mais on le signale fort)."""
    snap = f"{UPPER_DS}@presfs-{old_crc or 'unknown'}-{int(time.time())}"
    notes = []
    if run(["zfs", "snapshot", snap]) == 0:
        notes.append(f"snapshot {snap}")
        dest = f"data_pool/archives/rootfs-presfs-{old_crc or 'unknown'}"
        # send|recv vers data_pool (durable) ; best-effort
        rc = os.system(f"zfs send {snap} | zfs recv -F {dest} "
                       f">/dev/null 2>&1")
        notes.append(f"send -> {dest} ({'ok' if rc == 0 else 'ECHEC'})")
    else:
        notes.append(f"snapshot {snap} ECHEC (ancien upper NON sauvegarde)")
    # SUPPRIMER ENTIEREMENT upper/ et work/ (pas seulement leur contenu). Le
    # repertoire upperdir LUI-MEME porte des xattr overlay du PRECEDENT lower
    # (trusted.overlay.origin/impure) ; se contenter de vider le contenu les
    # garde -> overlayfs 'failed to verify upper root origin' -> ESTALE (116) au
    # montage avec le NOUVEAU sfs. On recree des repertoires NEUFS (inode
    # vierge, aucun xattr overlay).
    for sub in ("upper", "work"):
        shutil.rmtree(f"/mnt/ovl/{sub}", ignore_errors=True)
    os.makedirs("/mnt/ovl/upper", exist_ok=True)
    os.makedirs("/mnt/ovl/work", exist_ok=True)
    _strip_overlay_xattrs("/mnt/ovl/upper")     # ceinture + bretelles
    write_sfs_marker("/mnt/ovl", sfs_crc)
    return " | ".join(notes)


def main():
    os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"

    # --- 1. pseudo-FS --------------------------------------------------------
    mount("proc", "/proc", "proc")
    mount("sysfs", "/sys", "sysfs")
    mount("devtmpfs", "/dev", "devtmpfs")
    try:                                   # rebranche le stdio sur la console
        cfd = os.open("/dev/console", os.O_RDWR)
        for std in (0, 1, 2):
            os.dup2(cfd, std)
        if cfd > 2:
            os.close(cfd)
    except OSError:
        pass
    for d in ("/run", "/dev/pts"):
        os.makedirs(d, exist_ok=True)
    mount("devpts", "/dev/pts", "devpts")
    mount("tmpfs", "/run", "tmpfs")
    log("pseudo-FS montes")

    # debug : si 'debug' dans la cmdline, ne pas rebooter au panic (ecran
    # lisible) et logs verbeux. /proc/sys/kernel/panic = 0 -> attend indefiniment.
    if debug_enabled():
        log("MODE DEBUG actif (cmdline) : logs verbeux, pas de reboot au panic")
        try:
            with open("/proc/sys/kernel/panic", "w") as f:
                f.write("0")
        except OSError:
            pass
    debug_log(f"cmdline = {cmdline()}")
    debug_shell("pseudofs")

    # --- 1bis. STREAM CONSOLE DE BOOT (OPTIONNEL) --------------------------
    # Le stream (ffmpeg) est une commodite, PAS une necessite. Il a crashe tot
    # (general protection fault) et masquait les vrais problemes de montage. Il
    # ne se lance donc QUE si 'stream' est present dans la cmdline. Par defaut
    # (debug), pas de ffmpeg -> un point de crash en moins, logs a l'ecran.
    boot_stream = None
    if "stream" in cmdline().split():
        log("stream demande (cmdline) : demarrage ffmpeg...")
        boot_stream = start_boot_stream(read_yt_key())
        if boot_stream is None:
            log("(stream demande mais non demarre : cle/ffmpeg manquant)")
    else:
        log("stream console desactive (ajoute 'stream' a la cmdline pour l'activer)")

    # --- 2. ZFS (famille de modules, dans l'ordre des dependances) ----------
    debug_shell("zfs")
    extra = f"/lib/modules/{KVER}/extra"
    try:
        with open(f"{extra}/zfs_load_order") as f:
            zmods = [l.strip() for l in f if l.strip()]
    except OSError:
        zmods = ["spl", "zfs"]              # repli si l'ordre n'a pas ete ecrit
    for mod in zmods:
        path = f"{extra}/{mod}.ko"
        fatal = (mod == "zfs")             # seul zfs (le dernier) est bloquant
        try:
            load_module(path)
        except OSError as e:
            if fatal:
                die(f"chargement {mod}.ko: {e}\n"
                    f"  (deps noyau de ZFS en =m au lieu de =y ? cf. README)")
            log(f"{mod} non charge ({e})")
    log(f"zfs charge (modules: {', '.join(zmods)})")

    # --- 3. import pool : fast_pool, sinon SECOURS sur boot_pool ------------
    rescue = False
    sfs_ds, rootfs_name = SFS_DS, ROOTFS_SFS
    if import_pool(POOL) == 0:
        health_check()
        # mount_zfs_dataset gere mountpoint != legacy (fast_pool/sfs = /fast_pool/sfs)
        if not mount_zfs_dataset(SFS_DS, "/mnt/sfs"):
            log(f"!! {SFS_DS} non montable malgre l'import -> SECOURS")
            rescue = True
    else:
        log(f"!! import {POOL} echoue (NVMe en panne ? stripe = perte totale)")
        rescue = True

    if rescue:
        log("=" * 56)
        log("MODE SECOURS : fast_pool indisponible.")
        log("  -> rootfs de base depuis boot_pool, overlays NON restaures.")
        log("  -> les donnees de fast_pool (var/rootfs/tmp) sont perdues.")
        log("  -> remplace le NVMe et restaure depuis boot_pool/data_pool.")
        log("=" * 56)
        if import_pool(RESCUE_POOL) != 0:
            die(f"import {RESCUE_POOL} (secours) echoue : aucun rootfs disponible")
        if not mount_zfs_dataset(RESCUE_SFS_DS, "/mnt/sfs"):
            die(f"mount {RESCUE_SFS_DS} (secours) echoue")
        sfs_ds, rootfs_name = RESCUE_SFS_DS, RESCUE_ROOTFS
    log(f"source rootfs : {sfs_ds}/{rootfs_name}"
        + ("  [SECOURS]" if rescue else ""))

    # --- 3bis. import data_pool (donnees : home, log, archives) -------------
    # Non bloquant : si data_pool manque, le systeme boote quand meme (mais sans
    # /home etc.). Monte recursivement plus tard, apres switch_root, ou ici sous
    # NEWROOT une fois l'overlay pret. On l'importe maintenant (rapide) pour que
    # les datasets soient disponibles ; le montage ordonne se fait apres overlay.
    if not rescue:
        if import_pool(DATA_POOL) == 0:
            log(f"{DATA_POOL} importe (datasets disponibles pour montage ordonne)")
        else:
            log(f"[!] import {DATA_POOL} echoue -> /home et donnees indisponibles "
                f"(non bloquant pour le boot)")

    # --- 3ter. import boot_pool (manager : manifest/journal + images) -------
    # L'import reste valide a travers switch_root (etat noyau). Sans lui, le
    # manager (boot_pool/manager) est indisponible booted -> registre/journal/
    # boot_confirm muets, et boot_pool/images (secours) absent. Non bloquant.
    if not rescue:
        if import_pool(BOOT_POOL) == 0:
            log(f"{BOOT_POOL} importe (manager + images disponibles)")
        else:
            log(f"[!] import {BOOT_POOL} echoue -> manager indisponible "
                f"(non bloquant pour le boot)")

    # --- 4. overlay racine : lower=rootfs.sfs (ro) + upper -----------------
    debug_shell("overlay")
    # Normal : upper = dataset persistant fast_pool/rootfs (systeme mutable).
    # CORRUPTION (UPPER_DS existe mais montage/ecriture impossible) : on NE
    # monte PAS le dataset corrompu -> bascule DEGRADE LECTURE SEULE (upper
    # tmpfs jetable : systeme utilisable, rien ecrit sur le dataset corrompu),
    # rapport + temoin. Absent (1er boot) -> tmpfs simple. Secours -> tmpfs.
    degraded_reasons = []
    for d in ("/mnt/lower", "/mnt/ovl", NEWROOT):
        os.makedirs(d, exist_ok=True)
    use_persistent_upper = False
    if rescue:
        degraded_reasons.append("fast_pool absent (disque ?) : rootfs depuis boot_pool")
    elif ds_exists(UPPER_DS):
        # canmount=noauto : l'upper ne doit JAMAIS etre monte automatiquement a
        # son mountpoint naturel (/fast_pool/rootfs) -- il est reserve a
        # l'overlay (on le monte explicitement sur /mnt/ovl). Un double montage
        # creerait un acces concurrent au meme dataset (incoherences si ecriture).
        run(["zfs", "set", "canmount=noauto", UPPER_DS])
        # s'il etait deja monte a son emplacement naturel (boot precedent, outil),
        # le demonter pour eviter le double-acces avant de le monter sur /mnt/ovl.
        nat = zfs_mountpoint(UPPER_DS)
        if nat and nat not in ("legacy", "none") and os.path.ismount(nat):
            run(["umount", nat])
        # mount_zfs_dataset gere mountpoint != legacy (fast_pool/rootfs =
        # /fast_pool/rootfs). L'ancien 'mount.zfs UPPER_DS /mnt/ovl' direct
        # echouait sur un dataset non-legacy -> faux 'ne se monte pas'.
        if not mount_zfs_dataset(UPPER_DS, "/mnt/ovl"):
            degraded_reasons.append(
                f"{UPPER_DS} existe mais NE SE MONTE PAS (corruption ?) "
                f"-> non monte, upper volatile")
        elif not writable_test("/mnt/ovl"):
            run(["umount", "/mnt/ovl"])
            degraded_reasons.append(
                f"{UPPER_DS} monte mais NON INSCRIPTIBLE (corruption/RO ?) "
                f"-> demonte, upper volatile")
        else:
            use_persistent_upper = True
            log(f"upper persistant : {UPPER_DS} (sain, inscriptible)")
            os.makedirs("/mnt/ovl/upper", exist_ok=True)   # avant le check marqueur
            os.makedirs("/mnt/ovl/work", exist_ok=True)
            # changement de sfs ? CRC du sfs courant vs marqueur dans l'upper
            crc = sfs_crc32(f"/mnt/sfs/{rootfs_name}")
            stale, old = upper_stale("/mnt/ovl", crc)
            if stale:
                log(f"!! rootfs.sfs A CHANGE (crc {old} -> {crc}) : "
                    f"l'upper persistant est PERIME pour ce sfs.")
                log("   -> snapshot de l'ancien upper + upper NEUF "
                    "(les modifs repartent du nouveau sfs)")
                bilan = snapshot_and_reset_upper(old, crc)
                log(f"   {bilan}")
            elif not old:
                write_sfs_marker("/mnt/ovl", crc)   # 1er boot : poser le marqueur
                log(f"upper neuf, marqueur sfs pose (crc {crc})")
            else:
                log(f"sfs inchange (crc {crc}) : upper persistant reutilise")
    else:
        degraded_reasons.append(
            f"{UPPER_DS} absent (1er boot ? cree-le : zfs create {UPPER_DS})")

    if not use_persistent_upper:
        mount("tmpfs", "/mnt/ovl", "tmpfs")        # upper jetable
        log("upper volatile : tmpfs (rien ne sera ecrit sur un dataset corrompu)")
    os.makedirs("/mnt/ovl/upper", exist_ok=True)
    os.makedirs("/mnt/ovl/work", exist_ok=True)
    sfs_path = f"/mnt/sfs/{rootfs_name}"
    # GARDE DEPENDANCE : /mnt/sfs doit etre un VRAI point de montage (le dataset
    # sfs monte), pas un repertoire vide. Sinon l'overlay s'appuierait sur un
    # lower fantome -> systeme casse silencieusement.
    if not os.path.ismount("/mnt/sfs"):
        die("DEPENDANCE MANQUANTE : /mnt/sfs n'est pas monte (le dataset sfs "
            "n'a pas ete monte). Impossible d'assembler l'overlay racine.\n"
            "  Verifie l'import du pool et le mountpoint du dataset sfs.")
    if not os.path.exists(sfs_path):
        # CAUSE FREQUENTE : l'image n'a jamais ete creee (build incomplet, ou
        # mksquashfs jamais lance). Sans lower, pas de rootfs -> on l'explique
        # clairement et on ouvre un shell de secours plutot qu'un ecran fige.
        die(f"IMAGE ROOTFS ABSENTE : {sfs_path} introuvable.\n"
            f"  Le systeme ne peut pas monter sa racine. Cree l'image :\n"
            f"  python3 sfs_build.py --rootfs-src <racine> "
            f"(ou first_boot.py --rootfs-src ...)\n"
            f"  puis verifie : ls -la /mnt/sfs/")
    try:
        ld = losetup(sfs_path, readonly=True)
        mount(ld, "/mnt/lower", "squashfs", MS_RDONLY)
        # index=off : DESACTIVE la verification d'origine de l'upper. Le DESIGN
        # echange le lower (rootfs.sfs) sous un upper PERSISTANT, ce qui est
        # incompatible avec 'index=on' (defaut effectif -> 'failed to verify
        # upper root origin') : index=on stocke dans l'upper le handle du lower
        # et le re-verifie au montage ; des que le sfs change, le handle est
        # perime -> ESTALE (116). index=off rend l'upper portable d'un sfs a
        # l'autre, et un upper peuple existant reste montable tel quel sur un
        # reboot a sfs inchange (le reset CRC32 ne s'execute que si le sfs change).
        mount("overlay", NEWROOT, "overlay", 0,
              "lowerdir=/mnt/lower,upperdir=/mnt/ovl/upper,workdir=/mnt/ovl/work,"
              "index=off")
    except OSError as e:
        die(f"overlay echoue: {e}")
    log(f"overlay rootfs assemble sur {NEWROOT}")

    # --- 4bis. couches persistantes : var/log + usr-src vers NEWROOT ---------
    # /var lui-meme reste dans l'overlay rootfs (PAS un dataset). Seuls
    # fast_pool/log (-> NEWROOT/var/log) et fast_pool/usr-src (-> NEWROOT/mnt/
    # usr-src, pour NE PAS masquer /usr/src de Gentoo) sont des datasets a
    # remonter a leur place finale. Approche EPUREE : on remonte explicitement
    # ces deux-la, sans mounts.map ni modes dynamiques (une seule convention).
    # var/log tolere un contenu existant (overlay) ; usr-src non.
    if use_persistent_upper:
        for ds, sub, allow_ne in ((LOG_DS, "var/log", True),
                                  (USRSRC_DS, "mnt/usr-src", False)):
            if not ds_exists(ds):
                log(f"{ds} absent (cree-le : zfs create {ds})")
                continue
            tgt = f"{NEWROOT}/{sub}"
            if not remount_to(ds, tgt, allow_nonempty=allow_ne):
                degraded_reasons.append(
                    f"{ds} existe mais remontage vers {tgt} echoue (corruption ?)")
    elif not rescue:
        degraded_reasons.append("couches var/log + usr-src non montees (upper degrade)")

    # --- 4bis-2. data_pool : montage recursif ORDONNE sous NEWROOT ----------
    # On respecte le MOUNTPOINT SYSTEME de chaque dataset : data_pool/home
    # (mountpoint=/home) -> NEWROOT/home, data_pool/modeles (mountpoint=/...) ->
    # NEWROOT/..., etc. (avant : tout sous NEWROOT/mnt/data, donc /home tombait
    # sur l'upper volatil et les donnees utilisateur n'etaient pas persistantes).
    # Parent monte avant enfant. Non bloquant : le systeme boote meme si
    # data_pool est partiel (consigne en degrade pour les datasets critiques).
    if not rescue and pool_imported(DATA_POOL):
        log(f"montage recursif ordonne de {DATA_POOL} sous {NEWROOT} "
            "(mountpoints systeme respectes)...")
        ok_ds, failed_ds = mount_pool_recursive(DATA_POOL, NEWROOT,
                                                respect_mountpoint=True)
        if failed_ds:
            log(f"[!] {len(failed_ds)} dataset(s) data_pool non monte(s) : "
                f"{', '.join(sorted(failed_ds))}")
            # les datasets data_pool sont non-critiques (cf. infra.conf) -> warning
        else:
            log(f"data_pool monte ({len(ok_ds)} datasets, ordre parent->enfant OK)")

    # --- 4ter. mode degrade : rapport + temoin (session de reparation) ------
    degraded = rescue or bool(degraded_reasons)
    if degraded:
        try:
            os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
            with open(f"{NEWROOT}/etc/rescue-mode", "w") as f:
                f.write("degrade\n")
            with open(f"{NEWROOT}/etc/degraded-report", "w") as f:
                f.write("=== SYSTEME EN MODE DEGRADE (LECTURE SEULE) ===\n\n")
                f.write("Aucune donnee persistante n'est ecrite (upper volatile).\n")
                f.write("La session normale est REMPLACEE par un shell de "
                        "reparation.\n\nCauses detectees :\n")
                for r in degraded_reasons:
                    f.write(f"  - {r}\n")
                f.write("\nActions : zpool status -v ; zfs list ; "
                        "verifier/remplacer le disque ; restaurer depuis "
                        "boot_pool/data_pool.\n")
        except OSError as e:
            log(f"rapport degrade non ecrit ({e})")
        log("!! MODE DEGRADE : " + " | ".join(degraded_reasons))

    # report sante -> NEWROOT/etc (survit au switch_root ; /run sera masque)
    try:
        if os.path.exists("/run/health.json"):
            os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
            with open("/run/health.json") as src, \
                 open(f"{NEWROOT}/etc/health.json", "w") as dst:
                dst.write(src.read())
    except OSError as e:
        log(f"health.json non recopie sous NEWROOT ({e})")

    # --- 5. modules.sfs dans le futur rootfs (sous NEWROOT) -----------------
    msfs = f"/mnt/sfs/{MODULES_SFS}"
    if os.path.exists(msfs):
        tgt = f"{NEWROOT}/lib/modules/{KVER}"
        os.makedirs(tgt, exist_ok=True)
        try:
            ldm = losetup(msfs, readonly=True)
            mount(ldm, tgt, "squashfs", MS_RDONLY)
            log("modules.sfs monte dans le rootfs")
        except OSError as e:
            log(f"modules.sfs non monte ({e})")

    # --- 6. reseau statique (souvent deja fait par ip= cmdline) -------------
    iface = None
    for n in sorted(os.listdir("/sys/class/net")):
        if n == "lo":
            continue
        if os.path.exists(f"/sys/class/net/{n}/device"):
            iface = n
            break
    if iface:
        run(["ip", "link", "set", iface, "up"])
        run(["ip", "addr", "add", IP_ADDR, "dev", iface])   # echoue sans danger si ip= deja la
        run(["ip", "route", "add", "default", "via", GATEWAY])
        os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
        with open(f"{NEWROOT}/etc/resolv.conf", "w") as f:
            f.write(f"nameserver {DNS}\n")
        log(f"reseau: {iface} {IP_ADDR} gw {GATEWAY} dns {DNS}")
    else:
        log("aucune interface detectee (peut-etre deja via ip= cmdline)")

    # --- 7. handoff du stream initramfs -> NEWROOT/etc (survit au switch_root)
    # session_launch lira ces fichiers pour tuer ce ffmpeg et basculer sur la
    # capture wayland une fois le compositeur pret.
    for name in ("initramfs-stream.pid", "yt.key"):
        src = f"/run/{name}"
        if os.path.exists(src):
            try:
                os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
                with open(src) as s, open(f"{NEWROOT}/etc/{name}", "w") as d:
                    d.write(s.read())
            except OSError as e:
                log(f"handoff {name} non recopie ({e})")

    # propager la cle YouTube de l'INITRAMFS vers le rootfs, MEME si le stream
    # n'a pas demarre (sinon on la perd au switch_root). Persistance : si l'upper
    # est persistant, elle restera dans /etc du rootfs pour les prochains boots.
    key = read_yt_key()                          # lit /etc/yt.key de l'initramfs
    dst = f"{NEWROOT}/etc/yt.key"
    if key and not os.path.exists(dst):
        try:
            os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
            with open(dst, "w") as f:
                f.write(key + "\n")
            os.chmod(dst, 0o600)
            log("cle YouTube propagee vers le rootfs (/etc/yt.key)")
        except OSError as e:
            log(f"cle YouTube non propagee ({e})")

    # --- 8. switch_root en Python (PAS pivot_root : on est en rootfs) -------
    debug_shell("switch")
    nxt = f"{NEWROOT}/sbin/session_launch.py"
    if not os.path.exists(nxt):
        die(f"{nxt} absent")
    log("switch_root -> /sbin/session_launch.py")

    # CRITIQUE : deplacer les pseudo-FS dans le nouveau root AVANT de basculer.
    # Sinon le nouveau / a un /dev VIDE -> pas de /dev/null -> python/session_launch
    # echoue ('/dev/null no such file or directory') -> PID1 mort -> kernel panic.
    # C'est ce que fait le vrai 'switch_root' (busybox) en interne.
    for pf in ("dev", "proc", "sys", "run"):
        src = f"/{pf}"
        dst = f"{NEWROOT}/{pf}"
        if os.path.ismount(src):
            os.makedirs(dst, exist_ok=True)
            try:
                mount(src, dst, "", MS_MOVE)
                log(f"  {src} --move-> {dst}")
            except OSError as e:
                # repli : si MS_MOVE echoue, tenter un bind (mieux que rien)
                log(f"  [!] move {src} echoue ({e}) -> bind")
                try:
                    mount(src, dst, "", MS_BIND)
                except OSError as e2:
                    log(f"  [!] bind {src} echoue aussi: {e2}")

    # garantir /dev/null et /dev/console meme si devtmpfs incomplet (filet)
    devnull = f"{NEWROOT}/dev/null"
    if not os.path.exists(devnull):
        try:
            os.mknod(devnull, 0o20666, os.makedev(1, 3))
            log("  /dev/null cree (filet, devtmpfs incomplet)")
        except OSError:
            pass

    os.chdir(NEWROOT)
    mount(".", "/", "", MS_MOVE)            # deplace l'overlay sur /
    os.chroot(".")
    os.chdir("/")
    py = "/usr/bin/python3"
    try:
        os.execv(py, [py, "/sbin/session_launch.py"])
    except OSError as e:
        die(f"execv session_launch impossible: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:               # filet : ne jamais quitter PID 1 sans rescue
        die(f"exception non geree: {exc!r}")

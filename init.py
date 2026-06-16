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

# --- mode secours (degrade niveau 1) -----------------------------------------
# fast_pool est un STRIPE (zero redondance) : si un NVMe lache, le pool est
# perdu. On bascule alors sur le master durable de boot_pool (mirror). Les
# overlays (upper) de fast_pool sont definitivement perdus -> tmpfs neuf, et on
# l'affiche clairement. Ce n'est pas une continuite, c'est un systeme pour
# diagnostiquer/restaurer.
RESCUE_POOL    = "boot_pool"
RESCUE_SFS_DS  = f"{RESCUE_POOL}/images"
RESCUE_ROOTFS  = "rootfs.sfs"

# --- couches persistantes (datasets fast_pool montes au boot) ----------------
# upper de l'overlay racine + montages directs. En mode SECOURS (fast_pool
# absent), aucun n'est disponible -> on retombe sur un upper tmpfs volatile.
UPPER_DS   = f"{POOL}/rootfs"     # upper de l'overlay racine (systeme mutable)
VAR_DS     = f"{POOL}/var"        # monte sur NEWROOT/var
LOG_DS     = f"{POOL}/log"        # monte sur NEWROOT/var/log (replique vers data_pool)
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
MS_MOVE   = 8192
LOOP_CTL_GET_FREE = 0x4C82
LOOP_SET_FD       = 0x4C00
NR_finit_module   = 313        # x86_64
EEXIST = 17

libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
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


def mount(src, tgt, fstype, flags=0, data=None):
    s = src.encode() if src else None
    t = tgt.encode()
    f = fstype.encode() if fstype else None
    d = data.encode() if data else None
    if libc.mount(s, t, f, ctypes.c_ulong(flags), d) != 0:
        e = ctypes.get_errno()
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
    # vider le contenu de l'upper (upperdir + workdir), garder le dataset
    for sub in ("upper", "work"):
        d = f"/mnt/ovl/{sub}"
        try:
            if os.path.isdir(d):
                for name in os.listdir(d):
                    p = os.path.join(d, name)
                    if os.path.isdir(p) and not os.path.islink(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
        except OSError:
            pass
    os.makedirs("/mnt/ovl/upper", exist_ok=True)
    os.makedirs("/mnt/ovl/work", exist_ok=True)
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

    # --- 1bis. STREAM CONSOLE DE BOOT (le plus tot possible) ----------------
    # On streame des maintenant : tout le reste du boot (ZFS, overlay...) sera
    # visible en direct sur YouTube. ffmpeg tourne en tache de fond.
    boot_stream = start_boot_stream(read_yt_key())
    if boot_stream is None:
        log("(pas de stream initramfs ; demarrage normal)")

    # --- 2. ZFS (famille de modules, dans l'ordre des dependances) ----------
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
    if run(["zpool", "import", "-N", "-f", "-d", "/dev", POOL]) == 0:
        health_check()
        os.makedirs("/mnt/sfs", exist_ok=True)
        if run(["mount.zfs", SFS_DS, "/mnt/sfs"]) != 0:
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
        if run(["zpool", "import", "-N", "-f", "-d", "/dev", RESCUE_POOL]) != 0:
            die(f"import {RESCUE_POOL} (secours) echoue : aucun rootfs disponible")
        os.makedirs("/mnt/sfs", exist_ok=True)
        if run(["mount.zfs", RESCUE_SFS_DS, "/mnt/sfs"]) != 0:
            die(f"mount {RESCUE_SFS_DS} (secours) echoue")
        sfs_ds, rootfs_name = RESCUE_SFS_DS, RESCUE_ROOTFS
    log(f"source rootfs : {sfs_ds}/{rootfs_name}"
        + ("  [SECOURS]" if rescue else ""))

    # --- 4. overlay racine : lower=rootfs.sfs (ro) + upper -----------------
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
        if run(["mount.zfs", UPPER_DS, "/mnt/ovl"]) != 0:
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
    try:
        ld = losetup(f"/mnt/sfs/{rootfs_name}", readonly=True)
        mount(ld, "/mnt/lower", "squashfs", MS_RDONLY)
        mount("overlay", NEWROOT, "overlay", 0,
              "lowerdir=/mnt/lower,upperdir=/mnt/ovl/upper,workdir=/mnt/ovl/work")
    except OSError as e:
        die(f"overlay echoue: {e}")
    log(f"overlay rootfs assemble sur {NEWROOT}")

    # --- 4bis. couches persistantes (normal + upper persistant sain) -------
    # var, log, usr-src : datasets ZFS directs. Corruption d'un dataset EXISTANT
    # = on ne le monte pas + degrade (pas de die). Absent = non bloquant.
    if use_persistent_upper:
        for ds, sub, critical in ((VAR_DS, "var", True),
                                  (LOG_DS, "var/log", False),
                                  (USRSRC_DS, "usr/src", False)):
            tgt = f"{NEWROOT}/{sub}"
            os.makedirs(tgt, exist_ok=True)
            if ds_exists(ds):
                if run(["mount.zfs", ds, tgt]) != 0:
                    degraded_reasons.append(
                        f"{ds} existe mais ne se monte pas (corruption ?)")
                elif critical and not writable_test(tgt):
                    run(["umount", tgt])
                    degraded_reasons.append(
                        f"{ds} non inscriptible (corruption ?) -> demonte")
                else:
                    log(f"persistant : {ds} -> /{sub}")
            else:
                log(f"{ds} absent (cree-le : zfs create {ds})")
    elif not rescue:
        degraded_reasons.append("couches var/log/usr-src non montees (upper degrade)")

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

    # --- 8. switch_root en Python (PAS pivot_root : on est en rootfs) -------
    nxt = f"{NEWROOT}/sbin/session_launch.py"
    if not os.path.exists(nxt):
        die(f"{nxt} absent")
    log("switch_root -> /sbin/session_launch.py")
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

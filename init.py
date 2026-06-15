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
import subprocess
import sys

# --- parametres --------------------------------------------------------------
KVER        = os.uname().release
POOL        = "fast_pool"
SFS_DS      = f"{POOL}/sfs"
ROOTFS_SFS  = "rootfs.sfs"
MODULES_SFS = f"modules-{KVER}.sfs"
NEWROOT     = "/mnt/root"

IP_ADDR  = "192.168.1.10/24"   # redondant si ip= passe par la cmdline noyau
GATEWAY  = "192.168.1.1"
DNS      = "8.8.8.8"

YT_KEY = ""                    # vide -> stream demarre apres pivot (session_launch)
RTMP   = "rtmp://a.rtmp.youtube.com/live2"
FB     = "/dev/fb0"
FPS, VBR = "30", "4500k"

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

    # --- 2. ZFS (seul module a charger) -------------------------------------
    for mod, fatal in (("spl", False), ("zfs", True)):
        path = f"/lib/modules/{KVER}/extra/{mod}.ko"
        try:
            load_module(path)
        except OSError as e:
            if fatal:
                die(f"chargement {mod}.ko: {e}")
            log(f"{mod} non charge ({e})")
    log("zfs charge")

    # --- 3. import pool + montage du dataset --------------------------------
    if run(["zpool", "import", "-N", "-f", "-d", "/dev", POOL]) != 0:
        die(f"import {POOL} echoue")
    os.makedirs("/mnt/sfs", exist_ok=True)
    if run(["mount.zfs", SFS_DS, "/mnt/sfs"]) != 0:
        die(f"mount {SFS_DS} echoue")
    log(f"{SFS_DS} monte sur /mnt/sfs")

    # --- 4. overlay : lower=rootfs.sfs (ro, loop) + upper=tmpfs -------------
    for d in ("/mnt/lower", "/mnt/ovl", NEWROOT):
        os.makedirs(d, exist_ok=True)
    mount("tmpfs", "/mnt/ovl", "tmpfs")
    os.makedirs("/mnt/ovl/upper", exist_ok=True)
    os.makedirs("/mnt/ovl/work", exist_ok=True)
    try:
        ld = losetup(f"/mnt/sfs/{ROOTFS_SFS}", readonly=True)
        mount(ld, "/mnt/lower", "squashfs", MS_RDONLY)
        mount("overlay", NEWROOT, "overlay", 0,
              "lowerdir=/mnt/lower,upperdir=/mnt/ovl/upper,workdir=/mnt/ovl/work")
    except OSError as e:
        die(f"overlay echoue: {e}")
    log(f"overlay rootfs assemble sur {NEWROOT}")

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

    # --- 7. stream fbdev -> RTMP (si cle + ffmpeg) ; handoff via /etc -------
    if YT_KEY and os.path.exists("/usr/bin/ffmpeg"):
        cmd = ["/usr/bin/ffmpeg", "-nostdin", "-f", "fbdev", "-framerate", FPS,
               "-i", FB, "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
               "-c:v", "libx264", "-preset", "veryfast", "-b:v", VBR,
               "-maxrate", VBR, "-bufsize", "9000k", "-pix_fmt", "yuv420p",
               "-g", "60", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
               "-f", "flv", f"{RTMP}/{YT_KEY}"]
        logf = open("/run/stream.log", "wb")
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        os.makedirs(f"{NEWROOT}/etc", exist_ok=True)
        with open(f"{NEWROOT}/etc/initramfs-stream.pid", "w") as f:
            f.write(f"{proc.pid}\n")
        with open(f"{NEWROOT}/etc/yt.key", "w") as f:
            f.write(f"{YT_KEY}\n")
        log(f"stream initramfs demarre (pid {proc.pid})")

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

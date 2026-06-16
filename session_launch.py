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
    """Attend le socket wayland puis capture l'ecran vers RTMP."""
    if not key:
        return
    sock = os.path.join(RUNTIME_DIR, "wayland-0")
    for _ in range(20):
        if os.path.exists(sock):
            break
        time.sleep(0.5)
    env = dict(os.environ, WAYLAND_DISPLAY="wayland-0", XDG_RUNTIME_DIR=RUNTIME_DIR)
    url = f"{RTMP}/{key}"
    if which("wl-screenrec"):              # Intel VAAPI : le plus propre
        subprocess.Popen(
            ["wl-screenrec", "--codec", "hevc", "--ffmpeg-muxer", "flv", "-f", url],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif which("wf-recorder"):
        rec = subprocess.Popen(["wf-recorder", "-c", "rawvideo", "-f", "-"],
                               env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
        subprocess.Popen(
            ["ffmpeg", "-nostdin", "-f", "rawvideo", "-i", "-",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-c:v", "libx264", "-preset", "veryfast", "-b:v", VBR,
             "-maxrate", VBR, "-bufsize", "9000k", "-pix_fmt", "yuv420p",
             "-g", "60", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-f", "flv", url],
            stdin=rec.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("capture wayland demarree")


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


def main():
    os.environ.setdefault("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
    for src, tgt, fs in (("proc", "/proc", "proc"), ("sysfs", "/sys", "sysfs"),
                         ("devtmpfs", "/dev", "devtmpfs"), ("tmpfs", "/run", "tmpfs")):
        mount(src, tgt, fs)
    os.makedirs("/dev/pts", exist_ok=True)
    subprocess.run(["mount", "-t", "devpts", "devpts", "/dev/pts"],
                   stderr=subprocess.DEVNULL)

    # MODE DEGRADE : rapport + shell de reparation, pas de session normale
    if os.path.exists("/etc/rescue-mode"):
        log("mode degrade detecte -> reparation (session normale annulee)")
        degraded_repair()                  # ne revient pas

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    os.chmod(RUNTIME_DIR, 0o700)
    os.environ["XDG_RUNTIME_DIR"] = RUNTIME_DIR

    if which("seatd"):
        subprocess.Popen(["seatd", "-g", "video"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ["LIBSEAT_BACKEND"] = "seatd"
        time.sleep(1)
        log("seatd demarre")

    key = read_key()
    stop_initramfs_stream()

    # capture wayland en arriere-plan (attend le compositeur)
    if os.fork() == 0:
        start_wayland_stream(key)
        os._exit(0)

    # compositeur kiosk (remplace l'image du PID 1)
    if which("cage"):
        log("demarrage cage")
        os.execvp("cage", ["cage", "--", "foot"])
    elif which("sway"):
        log("demarrage sway")
        os.execvp("sway", ["sway"])
    else:
        log("aucun compositeur (cage/sway) -> shell")
        os.execvp("sh", ["sh"])


if __name__ == "__main__":
    main()

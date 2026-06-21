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

    # 3. eudev : peuple /dev dynamiquement (cree /dev/dri/card0 etc.).
    #    Sur Gentoo/OpenRC le daemon est udevd (fourni par sys-fs/eudev).
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

    # Compositeur kiosk. CRITIQUE : on ne fait PLUS 'execvp' direct (qui
    # remplacerait PID 1 par cage -> si cage echoue a creer son backend wlroots,
    # PID 1 meurt -> KERNEL PANIC). On lance en SOUS-PROCESSUS, on surveille, et
    # en cas d'echec on retombe sur un shell. PID 1 ne quitte JAMAIS.
    def run_compositor():
        if which("cage"):
            log("demarrage cage (compositeur kiosk)")
            return subprocess.run(["cage", "--", "foot"]).returncode
        if which("sway"):
            log("demarrage sway")
            return subprocess.run(["sway"]).returncode
        log("aucun compositeur (cage/sway) installe")
        return 127

    rc = run_compositor()
    if rc != 0:
        # Causes frequentes du 'unable to create the wlroots backend' :
        #  - nomodeset (profil safe) : pas de KMS -> pas de /dev/dri -> wlroots KO
        #  - /dev/dri/card0 absent ou droits manquants (seatd/groupe video)
        #  - GPU non initialise (i915/xe force_probe)
        log("=" * 56)
        log(f"COMPOSITEUR ECHEC (rc={rc}). Causes probables :")
        log("  - boote en 'nomodeset' (profil safe) ? -> pas de KMS, wlroots")
        log("    ne peut pas creer de backend DRM. Boote un profil avec KMS.")
        has_dri = os.path.exists("/dev/dri/card0")
        log(f"  - /dev/dri/card0 present : {has_dri}")
        if not has_dri:
            log("    -> AUCUN device DRM : c'est la cause. Verifie i915/xe et")
            log("       que tu n'es PAS en nomodeset.")
        log("  Bascule sur un SHELL de maintenance (PID 1 reste vivant).")
        log("=" * 56)
        # PID 1 doit survivre : on relance un shell en boucle (exit -> re-shell).
        while True:
            subprocess.run(["sh"])
            log("shell quitte ; relance (PID 1 doit rester vivant). "
                "Eteins via 'poweroff -f' si besoin.")


if __name__ == "__main__":
    main()

#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
build_initramfs.py — construit l'initramfs (cpio newc + zstd), 100% Python.

Embarque : CPython (interpreteur + stdlib + .so), busybox (shell de secours),
zpool/zfs/mount.zfs + libs, iproute2 (ip), spl.ko/zfs.ko (decompresses),
firmware rtl_nic, les noeuds /dev/console + /dev/null, et init.py -> /init.

A lancer en root. Variables surchargeables par l'environnement.
"""
import os
import glob
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

KVER = os.environ.get("KVER", os.uname().release)
OUT = os.environ.get("OUT", f"initramfs-{KVER}.zst")
INIT_SRC = os.environ.get("INIT_SRC", "./init.py")
FFMPEG_STATIC = os.environ.get("FFMPEG_STATIC", "/usr/local/bin/ffmpeg")
PYBIN = os.environ.get("PYBIN", "/usr/bin/python3")   # python SYSTEME (lancer hors venv)


def _is_real_interpreter(path):
    """Distingue le VRAI interpreteur du wrapper python-exec. Le wrapper fait
    ~10-30 Ko et ne depend PAS de libpython. Le vrai python est soit gros
    (statique, plusieurs Mo) soit lie a libpython3.X.so. Critere combine :
    taille >= 100 Ko OU dependance libpython (ldd). Le CHEMIN n'entre pas en
    compte (le vrai binaire vit legitimement sous /usr/lib/python-exec/)."""
    try:
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) >= 100 * 1024:
            return True
        # petit binaire : vrai seulement s'il depend de libpython
        out = subprocess.run(["ldd", path], capture_output=True,
                             text=True).stdout
        return "libpython" in out
    except OSError:
        return False


def _resolves_to_wrapper(path):
    """Le chemin (apres resolution des liens) mene-t-il au wrapper python-exec2c ?"""
    try:
        target = os.path.realpath(path)
        return "python-exec" in os.path.basename(target)
    except OSError:
        return True


def resolve_real_python(pybin):
    """Sur Gentoo, /usr/bin/python3 (generique) est un lien vers le wrapper
    python-exec2c. Le VRAI interpreteur est le binaire VERSIONNE /usr/bin/
    python3.X (gros ELF). On le cible directement.

    Strategie : determiner la version (X.Y) via le pybin, puis chercher
    /usr/bin/python3.X et /usr/lib/python-exec/python3.X/python3.X ; resoudre
    les liens (realpath) ; rejeter tout ce qui mene a python-exec2c ; retenir
    le premier vrai interpreteur (gros ELF / lie a libpython)."""
    # version X.Y du pybin (ex '3.14')
    ver = ""
    try:
        ver = subprocess.run(
            [pybin, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True).stdout.strip()
    except OSError:
        pass

    candidates = []
    if ver:
        # binaire VERSIONNE : c'est le vrai ELF sur Gentoo
        candidates.append(f"/usr/bin/python{ver}")
        candidates.append(f"/usr/lib/python-exec/python{ver}/python{ver}")
    # sys._base_executable en complement
    try:
        out = subprocess.run(
            [pybin, "-c", "import sys; print(sys._base_executable or '')"],
            capture_output=True, text=True).stdout.strip()
        if out:
            candidates.append(out)
    except OSError:
        pass
    # tous les binaires versionnes connus, en dernier recours
    import glob
    candidates += sorted(glob.glob("/usr/bin/python3.*"), reverse=True)
    candidates += sorted(glob.glob("/usr/lib/python-exec/python3.*/python3.*"),
                         reverse=True)

    for c in candidates:
        if not c or _resolves_to_wrapper(c):
            continue
        real = os.path.realpath(c)             # resoudre tous les liens
        if _is_real_interpreter(real):
            return real
    # rien trouve : repli (l'auto-test post-build avertira)
    return os.path.realpath(pybin)


DIRS = ["bin", "sbin", "etc", "proc", "sys", "dev", "run", "mnt",
        "lib", "lib64", "usr/lib", "usr/bin", "usr/sbin",
        f"lib/modules/{KVER}/extra", "lib/firmware/rtl_nic", "lib/firmware/i915"]

# Firmware a embarquer (motifs glob sous /lib/firmware) :
#   rtl_nic  -> NIC Realtek r8169 (couvre RTL8168 1G ET RTL8125 2.5G)
#   i915/tgl_* -> GuC + HuC (Rocket Lake reutilise Tiger Lake) -- xe les lit dans i915/
#   i915/rkl_* -> DMC (power management display, specifique Rocket Lake)
# On copie tous les suffixes de version pour ne dependre d'aucun nom precis.
FW_GLOBS = os.environ.get("FW_GLOBS",
                          "/lib/firmware/rtl_nic/rtl8168*:"
                          "/lib/firmware/rtl_nic/rtl8125*:"   # RTL8125B 2.5GbE
                          "/lib/firmware/i915/tgl_*:"
                          "/lib/firmware/i915/rkl_*").split(":")


def msg(s):
    print(f">> {s}", flush=True)


INFRA_CONF = os.environ.get("INFRA_CONF", "infra.conf")


def need_root():
    if os.geteuid() != 0:
        sys.exit("root requis (mknod, depmod)")


def which(name):
    # en chroot le PATH peut etre incomplet : on cherche d'abord dans les
    # dossiers systeme standards, puis on retombe sur shutil.which.
    for d in ("/sbin", "/usr/sbin", "/usr/bin", "/bin", "/usr/local/sbin",
              "/usr/local/bin"):
        c = os.path.join(d, name)
        if os.path.exists(c):
            return c
    return shutil.which(name)


def copy(src, stage):
    """Copie src sous stage en CONSERVANT son chemin (nom SONAME), contenu deref.
    Indispensable : le loader cherche libc.so.6, pas libc-2.39.so."""
    dst = stage + src
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if not os.path.exists(dst):
        shutil.copy2(src, dst, follow_symlinks=True)
    return dst


def copy_with_deps(binary, stage):
    src = which(binary) if "/" not in binary else binary
    if not src or not os.path.exists(src):
        msg(f"MANQUANT: {binary}")
        return False
    copy(src, stage)
    try:
        out = subprocess.run(["ldd", src], capture_output=True, text=True).stdout
    except Exception:
        out = ""
    for m in re.finditer(r"(/[^\s]+\.so[^\s]*)", out):
        lib = m.group(1)
        if os.path.exists(lib):
            copy(lib, stage)
    return True


def _copy_symlink(src, stage):
    """Recree le lien symbolique src sous stage (en preservant sa cible relative)."""
    dst = stage + src
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        target = os.readlink(src)
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(target, dst)
    except OSError as e:
        msg(f"lien {src} non recree ({e})")


def determine_epython():
    """Valeur de EPYTHON attendue par python-exec (ex 'python3.14'). Determinee
    depuis l'interpreteur de build."""
    try:
        v = subprocess.run([PYBIN, "-c",
                            "import sys; print('python3.%d' % sys.version_info[1])"],
                           capture_output=True, text=True).stdout.strip()
        return v or "python3.14"
    except OSError:
        return "python3.14"


def bundle_python(stage):
    """EMBARQUE TOUT l'ecosysteme Python de Gentoo, sans chercher a distinguer le
    wrapper du vrai interpreteur (source d'erreurs sans fin). On reproduit l'env
    Gentoo tel quel :
      - /usr/bin/python3 (wrapper python-exec2c) + tous les python3* de /usr/bin
      - TOUT /usr/lib/python-exec/ (le wrapper y cherche les vrais binaires)
      - les vrais binaires versionnes + leurs .so (dont libpython3.X.so.1.0)
      - la stdlib + lib-dynload
    Combinee a EPYTHON defini dans le lanceur /init (cf install), le wrapper
    fonctionne exactement comme sur le systeme."""
    import glob
    os.makedirs(f"{stage}/usr/bin", exist_ok=True)

    # 1. tous les binaires python de /usr/bin (wrapper python3 + versionnes) avec
    #    leurs dependances (.so). copy_with_deps suit les liens et fait le ldd.
    py_bins = sorted(set(glob.glob("/usr/bin/python3")
                         + glob.glob("/usr/bin/python3.*")))
    for b in py_bins:
        if os.path.lexists(b):
            # preserver le lien tel quel si c'en est un, sinon copier + deps
            if os.path.islink(b):
                _copy_symlink(b, stage)
            copy_with_deps(os.path.realpath(b), stage)
            # s'assurer que le NOM original existe aussi dans le stage
            copy_with_deps(b, stage)
    msg(f"binaires python embarques : {', '.join(os.path.basename(x) for x in py_bins)}")

    # 2. TOUT /usr/lib/python-exec/ (wrapper + liens + vrais binaires dedans)
    pexec = "/usr/lib/python-exec"
    if os.path.isdir(pexec):
        for root, _dirs, files in os.walk(pexec):
            for name in files:
                src = os.path.join(root, name)
                if os.path.islink(src):
                    _copy_symlink(src, stage)
                    tgt = os.path.realpath(src)
                    if os.path.isfile(tgt):
                        copy_with_deps(tgt, stage)
                elif os.path.isfile(src):
                    copy_with_deps(src, stage)
        msg("ecosysteme /usr/lib/python-exec/ embarque (wrapper fonctionnel)")
    else:
        msg("pas de /usr/lib/python-exec/ (pas de wrapper sur ce systeme, OK)")

    # 3. python-exec2c lui-meme (le vrai wrapper binaire) + sa conf
    for extra_bin in glob.glob("/usr/bin/python-exec*"):
        if os.path.lexists(extra_bin):
            copy_with_deps(os.path.realpath(extra_bin), stage)
    for conf in ("/etc/python-exec/python-exec.conf",):
        if os.path.exists(conf):
            copy(conf, stage)

    # 4. GARANTIE libpython (Gentoo shared) : repli glob si un ldd l'a ratee
    libpython_found = bool(glob.glob(f"{stage}/usr/lib*/libpython3.*.so*")
                           or glob.glob(f"{stage}/lib*/libpython3.*.so*"))
    if not libpython_found:
        for pat in ("/usr/lib*/libpython3.*.so*", "/lib*/libpython3.*.so*"):
            for lib in glob.glob(pat):
                copy(lib, stage)
                libpython_found = True
    msg("libpython embarquee" if libpython_found
        else "NOTE : pas de libpython detectee (python statique ?)")

    # 5. stdlib complete (inclut lib-dynload : _ctypes, fcntl, etc.)
    stdlib = sysconfig.get_path("stdlib")          # ex: /usr/lib/python3.14
    dst = stage + stdlib
    ignore = shutil.ignore_patterns("test", "tests", "idlelib", "tkinter",
                                    "turtledemo", "ensurepip", "lib2to3",
                                    "config-*", "__pycache__", "*.pyc")
    if not os.path.exists(dst):
        shutil.copytree(stdlib, dst, symlinks=True, ignore=ignore,
                        ignore_dangling_symlinks=True)
    # ldd des extensions compilees (libffi pour _ctypes, etc.)
    dynload = os.path.join(stdlib, "lib-dynload")
    if os.path.isdir(dynload):
        for f in os.listdir(dynload):
            if f.endswith(".so"):
                copy_with_deps(os.path.join(dynload, f), stage)
    msg(f"python embarque (ecosysteme complet, stdlib {stdlib})")


def bundle_busybox(stage):
    bb = which("busybox")
    if not bb:
        msg("busybox absent (pas de shell de secours)")
        return
    copy_with_deps(bb, stage)
    real = os.path.realpath(bb)
    for ap in ("sh", "mount", "umount", "ls", "cat", "dmesg"):
        link = f"{stage}/bin/{ap}"
        if not os.path.exists(link):
            os.symlink(real, link)
    msg("busybox (secours)")


def _modinfo(kver, mod, field):
    try:
        return subprocess.run(["modinfo", "-k", kver, "-F", field, mod],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return ""


def zfs_load_order(kver):
    """Ordre topologique (deps d'abord) de la famille zfs, en partant de 'zfs'.
    Robuste aux builds OpenZFS monolithiques (spl+zfs) comme splittes
    (spl,znvpair,zcommon,icp,...). Les deps built-in (=y, pas de .ko) sont
    ignorees : seuls les modules ayant un vrai .ko sont retenus."""
    order, seen = [], set()

    def visit(mod):
        if mod in seen:
            return
        seen.add(mod)
        for d in _modinfo(kver, mod, "depends").split(","):
            if d.strip():
                visit(d.strip())
        order.append(mod)

    visit("zfs")
    return order


def bundle_modules(stage):
    """Embarque la famille zfs (deps d'abord), decompresse si .zst/.xz
    (finit_module veut du brut), et ecrit l'ordre de chargement pour init.py."""
    extra = f"{stage}/lib/modules/{KVER}/extra"
    os.makedirs(extra, exist_ok=True)
    bundled = []
    for mod in zfs_load_order(KVER):
        path = _modinfo(KVER, mod, "filename") or \
            subprocess.run(["modinfo", "-k", KVER, "-n", mod],
                           capture_output=True, text=True).stdout.strip()
        if not path or path == "(builtin)" or not os.path.exists(path):
            continue                          # built-in ou absent -> rien a embarquer
        dst = os.path.join(extra, f"{mod}.ko")
        if path.endswith(".zst"):
            with open(dst, "wb") as o:
                subprocess.run(["zstd", "-d", "-c", path], stdout=o, check=True)
        elif path.endswith(".xz"):
            with open(dst, "wb") as o:
                subprocess.run(["xz", "-d", "-c", path], stdout=o, check=True)
        else:
            shutil.copy2(path, dst)
        bundled.append(mod)
    if "zfs" not in bundled:
        msg("ATTENTION: zfs.ko introuvable pour " + KVER +
            " -- le boot echouera (lance emerge -1 sys-fs/zfs-kmod)")
    # ordre de chargement consomme par init.py (deps d'abord, zfs en dernier)
    with open(os.path.join(extra, "zfs_load_order"), "w") as f:
        f.write("\n".join(bundled) + "\n")
    # depmod dans le stage
    subprocess.run(["depmod", "-b", stage, KVER], stderr=subprocess.DEVNULL)
    msg(f"modules zfs ({', '.join(bundled) or 'AUCUN'}) + ordre + depmod")


def bundle_firmware(stage):
    n = 0
    for pattern in FW_GLOBS:
        for f in glob.glob(pattern):
            if os.path.isfile(f):                # copy() deref les symlinks flottants
                copy(f, stage)
                n += 1
    if n == 0:
        msg("ATTENTION: aucun firmware copie — installe sys-kernel/linux-firmware")
    else:
        msg(f"firmware: {n} blobs (rtl_nic + i915 tgl/rkl pour GuC/HuC/DMC)")


def make_nodes(stage):
    import stat
    console = f"{stage}/dev/console"
    null = f"{stage}/dev/null"
    if not os.path.exists(console):
        os.mknod(console, stat.S_IFCHR | 0o600, os.makedev(5, 1))
    if not os.path.exists(null):
        os.mknod(null, stat.S_IFCHR | 0o666, os.makedev(1, 3))


def pack(stage, out):
    find = subprocess.Popen(["find", ".", "-print0"], cwd=stage,
                            stdout=subprocess.PIPE)
    cpio = subprocess.Popen(["cpio", "--null", "-o", "-H", "newc"],
                            cwd=stage, stdin=find.stdout,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    find.stdout.close()
    with open(out, "wb") as o:
        subprocess.run(["zstd", "-19", "-T0", "-f", "-c"],
                       stdin=cpio.stdout, stdout=o, check=True)
    cpio.stdout.close()


def main():
    need_root()
    stage = tempfile.mkdtemp(prefix="initramfs.")
    msg(f"staging : {stage}")
    try:
        for d in DIRS:
            os.makedirs(os.path.join(stage, d), exist_ok=True)
        make_nodes(stage)

        # loader dynamique
        for ld in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-x86-64.so.2"):
            if os.path.exists(ld):
                copy(ld, stage)

        bundle_python(stage)
        bundle_busybox(stage)        # AVANT l'auto-test : le lanceur /init = busybox sh
        # AUTO-TEST CRITIQUE : le python embarque s'execute-t-il REELLEMENT dans
        # l'environnement de l'initramfs ? (chroot = exactement ce que fait le
        # noyau au boot). Detecte le piege python-exec et les .so manquantes
        # AVANT de produire une image qui paniquerait en silence au boot.
        # On teste VIA L'ENVIRONNEMENT DU LANCEUR (EPYTHON defini), exactement
        # comme au boot : chroot + EPYTHON + /usr/bin/python3 (le wrapper).
        test_code = "import ctypes, fcntl, os, subprocess, sys; print('PYOK')"
        epython = determine_epython()
        env = dict(os.environ, EPYTHON=epython,
                   PATH="/usr/sbin:/usr/bin:/sbin:/bin",
                   LD_LIBRARY_PATH="/usr/lib64:/usr/lib:/lib64:/lib")
        try:
            r = subprocess.run(["chroot", stage, "/usr/bin/python3", "-c",
                                test_code], capture_output=True, text=True,
                               timeout=30, env=env)
            if "PYOK" not in r.stdout:
                sys.exit("ECHEC AUTO-TEST PYTHON dans l'initramfs :\n"
                         f"  EPYTHON={epython}\n"
                         f"  stdout: {r.stdout.strip()}\n"
                         f"  stderr: {r.stderr.strip()}\n"
                         "  -> wrapper/interpreteur/lib (.so) manquant malgre "
                         "l'embarquage complet. Build INTERROMPU.")
            msg(f"auto-test python (EPYTHON={epython}) : OK (imports critiques OK)")
        except FileNotFoundError:
            msg("ATTENTION : chroot indisponible, auto-test complet SAUTE.")
            # verif de secours : le wrapper a-t-il son ecosysteme ? (on l'assume
            # desormais : /usr/lib/python-exec doit etre present dans le stage)
            if os.path.isdir(f"{stage}/usr/lib/python-exec"):
                msg("verif secours : /usr/lib/python-exec present (wrapper OK)")
            elif os.path.exists(f"{stage}/usr/bin/python3"):
                msg("verif secours : python3 present (verifie le boot manuellement)")
            else:
                sys.exit("ECHEC : aucun python3 embarque. Build INTERROMPU.")
        except subprocess.TimeoutExpired:
            sys.exit("AUTO-TEST PYTHON : timeout (python bloque au demarrage ?)")
        # binaires CRITIQUES : leur absence rend l'initramfs non-bootable.
        # On ARRETE le build plutot que de produire une image cassee.
        manquants = []
        for b in ("zpool", "zfs", "mount.zfs", "ip"):
            if not copy_with_deps(b, stage):
                manquants.append(b)
        critiques = [b for b in manquants if b != "ip"]   # ip = degradable
        if critiques:
            raise SystemExit(
                "ECHEC build initramfs : binaires introuvables sur le systeme "
                f": {', '.join(critiques)}.\n"
                "  Verifie : which zpool zfs mount.zfs ; ls -la /sbin/zpool "
                "/sbin/zfs /sbin/mount.zfs\n"
                "  En chroot : sys-fs/zfs doit etre installe (emerge sys-fs/zfs) "
                "et /sbin dans le PATH.")
        if "ip" in manquants:
            msg("ATTENTION: 'ip' absent -> reseau initramfs indisponible "
                "(non bloquant pour le boot local)")
        bundle_modules(stage)
        bundle_firmware(stage)

        if FFMPEG_STATIC and os.access(FFMPEG_STATIC, os.X_OK):
            os.makedirs(f"{stage}/usr/bin", exist_ok=True)
            shutil.copy2(FFMPEG_STATIC, f"{stage}/usr/bin/ffmpeg")
            os.chmod(f"{stage}/usr/bin/ffmpeg", 0o755)
            # verif rapide : binaire statique (sinon il faudrait ses libs)
            try:
                ld = subprocess.run(["ldd", FFMPEG_STATIC],
                                    capture_output=True, text=True)
                if "not a dynamic executable" not in (ld.stdout + ld.stderr):
                    msg("ATTENTION: ffmpeg fourni semble DYNAMIQUE -- privilegie "
                        "un build statique (ffmpeg-git static), sinon libs manquantes")
            except Exception:
                pass
            msg("ffmpeg statique inclus (stream console de boot des l'init)")
        else:
            msg("ATTENTION: ffmpeg NON inclus -> PAS de stream pendant l'initramfs. "
                "Fournis FFMPEG_STATIC=/chemin/ffmpeg (build statique) pour "
                "streamer la console de boot des le chargement.")

        # cle de stream YouTube : deposee dans l'initramfs si fournie au build
        yt = os.environ.get("YT_KEY", "")
        if yt:
            with open(f"{stage}/etc/yt.key", "w") as f:
                f.write(yt.strip() + "\n")
            os.chmod(f"{stage}/etc/yt.key", 0o600)
            msg("cle YouTube deposee (/etc/yt.key, 0600)")
        else:
            msg("pas de YT_KEY au build -> stream initramfs inactif "
                "(depose /etc/yt.key dans l'initramfs ou passe YT_KEY=...)")

        # init.py ne lit PLUS de mounts.map : le montage (overlay + remontage
        # var/log + usr-src) est code en dur de facon epuree dans init.py. On
        # embarque en revanche infra.conf tel quel : utile pour les checkups et
        # la compilation automatique declenches depuis le systeme (PAS pour le
        # montage au boot). Optionnel : absent => l'initramfs boote quand meme.
        if os.path.exists(INFRA_CONF):
            try:
                shutil.copy2(INFRA_CONF, f"{stage}/etc/infra.conf")
                msg(f"infra.conf embarque ({INFRA_CONF} -> /etc/infra.conf)")
            except OSError as e:
                msg(f"infra.conf non embarque ({e}) -- non bloquant")

        if not os.path.exists(INIT_SRC):
            sys.exit(f"init introuvable: {INIT_SRC}")
        # /init = LANCEUR SHELL (busybox sh), PAS directement init.py. Raison :
        # le noyau lance /init avec un env MINIMAL (HOME=/ TERM=linux), donc
        # EPYTHON n'est pas defini -> le wrapper python-exec echouerait
        # ('no python-exec wrapper found'). Le lanceur exporte EPYTHON + PATH +
        # LD_LIBRARY_PATH puis exec le wrapper sur /init.py. Bonus : si python
        # echoue, le lanceur affiche l'erreur sur la console (plus de panic muet).
        epython = determine_epython()
        shutil.copy2(INIT_SRC, f"{stage}/init.py")     # le vrai code -> /init.py
        os.chmod(f"{stage}/init.py", 0o755)
        launcher = f"""#!/bin/busybox sh
# lanceur PID1 : etablit l'environnement Gentoo pour python-exec puis lance
# init.py. Genere par build_initramfs.py -- ne pas editer a la main.
export EPYTHON={epython}
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LD_LIBRARY_PATH=/usr/lib64:/usr/lib:/lib64:/lib
echo "[init-launcher] EPYTHON=$EPYTHON, lancement de python..." > /dev/kmsg 2>/dev/null
exec /usr/bin/python3 /init.py "$@"
# si exec echoue, on arrive ici :
echo "[init-launcher] ECHEC du lancement de python /init.py" > /dev/kmsg 2>/dev/null
exec /bin/busybox sh
"""
        dst_init = f"{stage}/init"
        with open(dst_init, "w") as f:
            f.write(launcher)
        os.chmod(dst_init, 0o755)
        msg(f"/init = lanceur shell (EPYTHON={epython}) -> /init.py")

        pack(stage, OUT)
        size = subprocess.run(["du", "-h", OUT], capture_output=True, text=True).stdout.split()[0]
        msg(f"OK -> {OUT}  ({size})")

        # verification POST-BUILD du contenu (sans booter) + checksum
        try:
            import initramfs_verify as iv
            crit_missing = False
            msg("verification du contenu de l'initramfs genere :")
            for lvl, line in iv.verify_contents(OUT):
                flag = "  !!" if (lvl == "crit" and "MANQUANT" in line) else "    "
                print(f"{flag} [{lvl}] {line}", flush=True)
                if lvl == "crit" and "MANQUANT" in line:
                    crit_missing = True
            sha = iv.image_sha256(OUT)
            msg(f"SHA-256 image : {sha}")
            if crit_missing:
                msg("ATTENTION: des fichiers CRITIQUES manquent -> NE PAS booter "
                    "cette image (corrige le build).")
            # enregistrer le checksum dans le registre (controle d'integrite)
            try:
                import kernel_registry
                kernel_registry.KernelRegistry().log_event(
                    "compile", KVER,
                    f"initramfs build sha256={sha[:16]} bootable="
                    f"{not crit_missing}")
            except Exception:
                pass
        except Exception as e:
            msg(f"verification post-build non effectuee ({e}) -- non bloquant")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    main()

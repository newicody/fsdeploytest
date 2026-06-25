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


def _copy_lib_with_soname(soname, real_path, stage):
    """Copie la lib reelle ET recree la chaine de liens SONAME qui pointe vers
    elle. ldd donne 'libpython3.14.so.0 => /usr/lib64/libpython3.14.so.1.0' : le
    binaire cherche le SONAME (gauche), le fichier reel a un autre nom (droite).
    Sans le lien, le loader ne trouve pas la lib au boot."""
    real = os.path.realpath(real_path)
    copy(real, stage)                          # le fichier reel (contenu)
    # recreer TOUS les intermediaires : real_path peut etre lui-meme un lien.
    # on cree le lien <dir>/<soname> -> basename(real), et le lien fourni par ldd.
    rdir = os.path.dirname(real)
    for linkname in {soname, os.path.basename(real_path)}:
        if not linkname or linkname == os.path.basename(real):
            continue
        link_src = os.path.join(rdir, linkname)
        dst_link = stage + link_src
        os.makedirs(os.path.dirname(dst_link), exist_ok=True)
        if not os.path.lexists(dst_link):
            try:
                os.symlink(os.path.basename(real), dst_link)
            except OSError:
                # repli : copie du contenu si le lien echoue
                copy(real, stage)


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
    # parser 'SONAME => /chemin/reel' pour recreer le lien SONAME ; et capturer
    # aussi les chemins absolus seuls (ld-linux, vdso ignore).
    for line in out.splitlines():
        line = line.strip()
        if "=>" in line:
            left, right = line.split("=>", 1)
            soname = left.strip()
            mreal = re.search(r"(/[^\s]+\.so[^\s]*)", right)
            if mreal and os.path.exists(mreal.group(1)):
                _copy_lib_with_soname(soname, mreal.group(1), stage)
        else:
            # ligne sans => : ex '/lib64/ld-linux-x86-64.so.2 (0x...)'
            mreal = re.search(r"(/[^\s]+\.so[^\s]*)", line)
            if mreal and os.path.exists(mreal.group(1)):
                copy(os.path.realpath(mreal.group(1)), stage)
                # recreer le lien sous son nom d'origine aussi
                orig = mreal.group(1)
                if os.path.realpath(orig) != orig:
                    dst_link = stage + orig
                    os.makedirs(os.path.dirname(dst_link), exist_ok=True)
                    if not os.path.lexists(dst_link):
                        try:
                            os.symlink(os.path.basename(os.path.realpath(orig)),
                                       dst_link)
                        except OSError:
                            pass
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

    # GARANTIE pour le fallback du lanceur : /usr/bin/python3 ET /usr/bin/
    # python3.<ver> doivent EXISTER et etre executables dans l'initramfs. On
    # determine le vrai ELF (en suivant les liens) et on le copie aux deux noms.
    # Ainsi le lanceur peut exec /usr/bin/python3.14 meme si le wrapper foire.
    ver = determine_epython().replace("python", "")     # ex '3.14'
    real_elf = None
    for cand in (f"/usr/bin/python{ver}", "/usr/bin/python3"):
        if os.path.lexists(cand):
            rp = os.path.realpath(cand)
            if os.path.isfile(rp):
                real_elf = rp
                break
    if real_elf:
        for name in (f"python{ver}", "python3"):
            tgt = f"{stage}/usr/bin/{name}"
            # NE PAS ecraser le wrapper python3 (il peut etre voulu) : on ne
            # force QUE le binaire versionne en copie directe du vrai ELF ; pour
            # python3, on laisse ce que bundle a mis SAUF s'il manque.
            if name != "python3" or not os.path.exists(tgt):
                try:
                    if os.path.lexists(tgt):
                        os.remove(tgt)
                    shutil.copy2(real_elf, tgt)
                    os.chmod(tgt, 0o755)
                except OSError as e:
                    msg(f"copie {name} : {e}")
        copy_with_deps(real_elf, stage)        # ses .so (libpython, etc.)
        msg(f"fallback garanti : /usr/bin/python{ver} = vrai ELF ({real_elf})")
    else:
        msg("ATTENTION : vrai ELF python introuvable pour le fallback !")

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


def _is_dynamic_elf(path):
    """True si l'ELF a un segment PT_INTERP (interpreteur dynamique = binaire
    DYNAMIQUE). False = statique. Methode fiable, langue-independante (lit les
    octets de l'en-tete ELF, ne parse aucun texte localise de ldd).
    Parse minimal de l'en-tete ELF + table des program headers."""
    try:
        with open(path, "rb") as f:
            data = f.read(64)
            if data[:4] != b"\x7fELF":
                return False                      # pas un ELF
            is64 = data[4] == 2                   # EI_CLASS: 2=64-bit
            le = data[5] == 1                     # EI_DATA: 1=little-endian
            order = "little" if le else "big"
            if is64:
                e_phoff = int.from_bytes(data[32:40], order)
                e_phentsize = int.from_bytes(data[54:56], order)
                e_phnum = int.from_bytes(data[56:58], order)
            else:
                e_phoff = int.from_bytes(data[28:32], order)
                e_phentsize = int.from_bytes(data[42:44], order)
                e_phnum = int.from_bytes(data[44:46], order)
            if not e_phoff or not e_phnum:
                return False
            with open(path, "rb") as f2:
                f2.seek(e_phoff)
                for _ in range(e_phnum):
                    ph = f2.read(e_phentsize)
                    if len(ph) < 4:
                        break
                    p_type = int.from_bytes(ph[0:4], order)
                    if p_type == 3:               # PT_INTERP = 3 -> dynamique
                        return True
            return False                          # aucun PT_INTERP -> statique
    except OSError:
        return False


def bundle_critical_libs(stage):
    """Embarque les bibliotheques chargees DYNAMIQUEMENT a l'execution (dlopen),
    que ldd NE liste PAS et que copy_with_deps rate donc :
      - libgcc_s.so.1 : requise par pthread_exit / gestion d'exceptions.
      - libnss_files / libnss_dns / libresolv : resolution noms/utilisateurs
        (busybox et python en ont besoin pour le reseau ; chargees via NSS donc
        invisibles a ldd).
    Pour CHAQUE lib, on copie le fichier reel ET on recree TOUTE la chaine de
    liens SONAME (ex libresolv.so -> libresolv.so.2 -> libresolv-2.XX.so), sinon
    le loader cherche le SONAME (.so.2) et ne le trouve pas."""
    import glob
    patterns = [
        "/usr/lib*/libgcc_s.so*", "/lib*/libgcc_s.so*",
        "/usr/lib/gcc/*/*/libgcc_s.so*",       # emplacement Gentoo (gcc-specific)
        "/usr/lib*/libnss_files.so*", "/lib*/libnss_files.so*",
        "/usr/lib*/libnss_dns.so*", "/lib*/libnss_dns.so*",
        "/usr/lib*/libnss_compat.so*", "/lib*/libnss_compat.so*",
        "/usr/lib*/libresolv.so*", "/lib*/libresolv.so*",
    ]
    found = []
    for pat in patterns:
        for lib in glob.glob(pat):
            if not (os.path.isfile(lib) or os.path.islink(lib)):
                continue
            real = os.path.realpath(lib)
            soname = re.sub(r"(\.so\.\d+).*$", r"\1", os.path.basename(real))
            # Copier le vrai fichier dans /usr/lib64 sous SON nom reel ET sous le
            # SONAME (lien). /usr/lib64 est dans LD_LIBRARY_PATH du lanceur, donc
            # la lib est TOUJOURS trouvable -- meme si Gentoo la range ailleurs
            # (ex libgcc_s.so.1 dans /usr/lib/gcc/<triplet>/<ver>/, hors path).
            dst_real = f"{stage}/usr/lib64/{os.path.basename(real)}"
            os.makedirs(os.path.dirname(dst_real), exist_ok=True)
            if not os.path.exists(dst_real):
                try:
                    shutil.copy2(real, dst_real)
                except (OSError, shutil.SameFileError):
                    pass
            # liens : SONAME + nom rencontre par le glob -> fichier reel
            for nm in {soname, os.path.basename(lib)}:
                if not nm or nm == os.path.basename(real):
                    continue
                link = f"{stage}/usr/lib64/{nm}"
                if not os.path.lexists(link):
                    try:
                        os.symlink(os.path.basename(real), link)
                    except OSError:
                        pass
            found.append(os.path.basename(lib))

    # GARANTIE supplementaire : faire le ldd de busybox ET python pour capturer
    # toute .so liee (avec recreation SONAME via copy_with_deps deja patchee).
    for binbase in ("/bin/busybox", "/sbin/busybox", "/usr/bin/python3"):
        if os.path.exists(stage + binbase):
            try:
                out = subprocess.run(["ldd", os.path.realpath(binbase)]
                                     if os.path.exists(os.path.realpath(binbase))
                                     else ["true"],
                                     capture_output=True, text=True).stdout
                for m in re.finditer(r"(/[^\s]+\.so[^\s]*)", out):
                    if os.path.exists(m.group(1)):
                        copy(os.path.realpath(m.group(1)), stage)
            except OSError:
                pass

    if any("libgcc_s" in f for f in found):
        msg(f"libs critiques (dlopen) embarquees : {', '.join(sorted(set(found)))}")
    else:
        msg("ATTENTION : libgcc_s.so.1 INTROUVABLE -> threads Python casses.")
    if any("libresolv" in f for f in found):
        msg(f"libresolv embarquee (+ liens SONAME)")
    else:
        msg("ATTENTION : libresolv INTROUVABLE -> reseau busybox/python casse.")
    return found


def bundle_busybox(stage):
    bb = which("busybox")
    if not bb:
        msg("busybox absent (pas de shell de secours NI de lanceur /init !)")
        return
    real = os.path.realpath(bb)
    copy_with_deps(real, stage)                # le binaire reel + ses .so
    # PLACER busybox a /bin/busybox ET /sbin/busybox (copie directe du vrai ELF,
    # pas un lien : le shebang '#!/bin/busybox' DOIT resoudre quel que soit
    # l'emplacement d'origine -- Gentoo le met souvent dans /sbin).
    for fixed in (f"{stage}/bin/busybox", f"{stage}/sbin/busybox"):
        os.makedirs(os.path.dirname(fixed), exist_ok=True)
        if not os.path.exists(fixed):
            shutil.copy2(real, fixed)
            os.chmod(fixed, 0o755)
    # applets en liens ABSOLUS vers /bin/busybox (garanti present) -> jamais de
    # lien casse meme si l'applet est dans /sbin et busybox 'logiquement' ailleurs.
    applets = ("sh", "mount", "umount", "ls", "cat", "dmesg", "mkdir", "mdev",
               "sleep", "timeout", "readlink", "mknod", "switch_root", "echo")
    for d in ("bin", "sbin"):
        for ap in applets:
            link = f"{stage}/{d}/{ap}"
            if not os.path.lexists(link):
                try:
                    os.symlink("/bin/busybox", link)   # ABSOLU
                except OSError:
                    pass
    msg(f"busybox -> /bin/busybox + /sbin/busybox + applets (origine {bb})")


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


def verify_bootable(stage):
    """Verifie les INVARIANTS de bootabilite AVANT de packager. Un initramfs qui
    rate l'un de ces points donne 'Failed to execute /init' ou un panic. On
    ARRETE le build plutot que de produire une image non-bootable."""
    errs = []
    init = os.path.join(stage, "init")
    # 1. /init existe et est executable
    if not os.path.isfile(init):
        errs.append("/init absent")
    elif not os.access(init, os.X_OK):
        errs.append("/init non executable")
    else:
        # 2. l'interpreteur du shebang existe et est executable
        with open(init, "rb") as f:
            first = f.readline().decode("latin1", "replace").strip()
        if first.startswith("#!"):
            interp = first[2:].strip().split()[0]      # ex /bin/busybox
            ip = stage + interp
            if not os.path.isfile(ip):
                errs.append(f"interpreteur du shebang absent : {interp} "
                            f"(shebang '{first}')")
            elif not os.access(ip, os.X_OK):
                errs.append(f"interpreteur du shebang non executable : {interp}")
    # 3. python utilisable (wrapper OU vrai binaire versionne)
    py_ok = any(os.path.isfile(os.path.join(stage, p)) for p in
                ("usr/bin/python3", "usr/bin/python3.14", "usr/bin/python3.13",
                 "usr/bin/python3.12"))
    if not py_ok:
        errs.append("aucun python dans /usr/bin")
    # 4. /init.py present (le vrai code)
    if not os.path.isfile(os.path.join(stage, "init.py")):
        errs.append("/init.py absent (le code de init)")
    # 5. busybox a /bin/busybox (le lanceur en depend via shebang ET commandes)
    if not os.path.isfile(os.path.join(stage, "bin/busybox")):
        errs.append("/bin/busybox absent (lanceur non executable)")

    if errs:
        sys.exit("INITRAMFS NON-BOOTABLE -- build INTERROMPU :\n  - "
                 + "\n  - ".join(errs))
    msg("verify_bootable : /init + interpreteur + python + busybox OK")


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
        bundle_critical_libs(stage)  # libgcc_s.so.1 (dlopen, pthread_exit) + nss
        # AUTO-TEST CRITIQUE : le python embarque s'execute-t-il REELLEMENT dans
        # l'environnement de l'initramfs ? (chroot = exactement ce que fait le
        # noyau au boot). Detecte le piege python-exec et les .so manquantes
        # AVANT de produire une image qui paniquerait en silence au boot.
        # On teste VIA L'ENVIRONNEMENT DU LANCEUR (EPYTHON defini), exactement
        # comme au boot : chroot + EPYTHON + /usr/bin/python3 (le wrapper).
        # inclut threading : un thread qui demarre+joint force le chargement de
        # libgcc_s.so.1 (pthread). Attrape le bug 'libgcc_s must be installed'
        # au BUILD plutot qu'au boot.
        test_code = ("import ctypes, fcntl, os, subprocess, sys, threading\n"
                     "t = threading.Thread(target=lambda: None); t.start(); t.join()\n"
                     "print('PYOK')")
        epython = determine_epython()
        env = dict(os.environ, EPYTHON=epython,
                   PATH="/usr/sbin:/usr/bin:/sbin:/bin",
                   LD_LIBRARY_PATH="/usr/lib64:/usr/lib:/lib64:/lib")
        # TEST BUSYBOX d'abord : 'busybox true' charge ses .so (dont libresolv).
        # Si une lib manque, busybox echoue avec 'error loading shared libraries'
        # -> on l'attrape ICI au lieu du panic au boot.
        try:
            rb = subprocess.run(["chroot", stage, "/bin/busybox", "true"],
                                capture_output=True, text=True, timeout=15, env=env)
            if rb.returncode != 0:
                sys.exit("ECHEC AUTO-TEST BUSYBOX dans l'initramfs :\n"
                         f"  stderr: {rb.stderr.strip()}\n"
                         "  -> une lib (.so) de busybox manque (ex libresolv.so.2). "
                         "Build INTERROMPU.")
            msg("auto-test busybox : OK (libs chargees)")
        except FileNotFoundError:
            pass                               # chroot indispo : on verra plus bas
        except subprocess.TimeoutExpired:
            sys.exit("AUTO-TEST BUSYBOX : timeout.")
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
            # DETECTION STATIQUE FIABLE (langue-independante) : on lit l'en-tete
            # ELF et on cherche un segment PT_INTERP (interpreteur dynamique).
            # Absent => binaire STATIQUE. (L'ancien test parsait le texte de ldd
            # 'not a dynamic executable', qui est traduit en FR -> faux positif.)
            if _is_dynamic_elf(FFMPEG_STATIC):
                msg("ATTENTION: ffmpeg fourni est DYNAMIQUE (a un interpreteur "
                    "ELF) -- ses .so manqueront dans l'initramfs. Fournis un "
                    "build STATIQUE (ffmpeg-git static) pour le stream de boot.")
                # embarquer ses libs malgre tout (best effort)
                copy_with_deps(os.path.realpath(FFMPEG_STATIC), stage)
            else:
                msg("ffmpeg statique inclus (verifie ELF: pas d'interpreteur) "
                    "-- stream console de boot OK")
        else:
            msg("ATTENTION: ffmpeg NON inclus -> PAS de stream pendant l'initramfs. "
                "Fournis FFMPEG_STATIC=/chemin/ffmpeg (build statique) pour "
                "streamer la console de boot des le chargement.")

        # zpool.cache : import INSTANTANE des pools au boot. Avec lui, import_pool
        # prend le chemin '-c /etc/zfs/zpool.cache' (aucun scan de devices) ; sans
        # lui, init.py retombe sur un scan '-d by-id/by-partuuid' (nettement plus
        # lent). On l'embarque s'il existe.
        cache_src = os.environ.get("ZPOOL_CACHE", "/etc/zfs/zpool.cache")
        if os.path.isfile(cache_src):
            os.makedirs(f"{stage}/etc/zfs", exist_ok=True)
            shutil.copy2(cache_src, f"{stage}/etc/zfs/zpool.cache")
            msg("zpool.cache embarque (/etc/zfs/zpool.cache -> import instantane)")
        else:
            msg("pas de zpool.cache au build -> import par scan (plus lent). "
                "Genere-le : zpool set cachefile=/etc/zfs/zpool.cache <pools>")

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
        # Le lanceur utilise 'busybox' SANS chemin absolu apres avoir mis
        # /bin et /sbin dans PATH : robuste meme si busybox est dans l'un ou
        # l'autre. Le shebang reste #!/bin/busybox (garanti par bundle_busybox
        # qui copie busybox a /bin/busybox ET /sbin/busybox).
        launcher = f"""#!/bin/busybox sh
# Lanceur PID1 (genere par build_initramfs.py -- ne pas editer).
export EPYTHON={epython}
export PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin
export LD_LIBRARY_PATH=/usr/lib64:/usr/lib:/lib64:/lib

# pseudo-FS minimaux + /tmp (sinon les redirections 2>/tmp/... echouent et
# faussent les tests : un test 'python -c ...' paraitrait echouer alors que
# c'est la redirection qui casse).
busybox mkdir -p /proc /sys /dev /tmp /run 2>/dev/null
busybox mount -t proc proc /proc 2>/dev/null
busybox mount -t sysfs sys /sys 2>/dev/null
busybox mount -t devtmpfs dev /dev 2>/dev/null
busybox mount -t tmpfs tmp /tmp 2>/dev/null

# NE JAMAIS rebooter sur panic : laisse l'ecran lisible (critique pour debugger)
echo 0 > /proc/sys/kernel/panic 2>/dev/null

say() {{ echo ""; echo "[init-launcher] $1"; echo "[init-launcher] $1" > /dev/kmsg 2>/dev/null; }}

CMDLINE=$(busybox cat /proc/cmdline 2>/dev/null)
say "lanceur demarre. EPYTHON=$EPYTHON"
say "cmdline: $CMDLINE"

# break=launcher : shell AVANT python (debug independant de python)
case "$CMDLINE" in
  *break=launcher*) say "BREAK=launcher : shell (exit pour continuer)"; busybox sh ;;
esac

# Test python SANS redirection fichier (la sortie va directement a la console).
# On teste le wrapper python3 ; s'il echoue, on bascule sur python3.14 (vrai
# ELF). NE PAS faire dependre la decision d'une redirection (cause du faux
# echec precedent : /tmp absent).
say "test de /usr/bin/python3..."
if /usr/bin/python3 -c "import sys"; then
  say "python3 OK, lancement de init.py..."
  exec /usr/bin/python3 /init.py "$@"
fi

say "python3 (wrapper) KO -> essai /usr/bin/python3.14 (vrai ELF)..."
if /usr/bin/python3.14 -c "import sys"; then
  say "python3.14 OK, lancement de init.py..."
  exec /usr/bin/python3.14 /init.py "$@"
fi

say "AUCUN python utilisable. Diagnostic :"
/usr/bin/python3.14 -c "import sys" || true
say "shell de secours (busybox) -- inspecte /usr/bin, /usr/lib64, ldd."
exec busybox sh
"""
        dst_init = f"{stage}/init"
        with open(dst_init, "w") as f:
            f.write(launcher)
        os.chmod(dst_init, 0o755)
        msg(f"/init = lanceur robuste (EPYTHON={epython}, panic=0, fallback python3.14)")

        verify_bootable(stage)        # ARRETE si /init/interpreteur/python/busybox KO
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

# Boot ZFS + stream YouTube — appliance Gentoo (100% Python)

Système minimal qui boote en **UEFI direct** (EFI stub, sans ZFSBootMenu) sur un
noyau dont l'**initramfs est piloté par Python** : il importe un pool ZFS, monte
un rootfs Gentoo en **squashfs + overlay**, configure le **réseau statique très
tôt**, **stream** le framebuffer puis Wayland vers YouTube, et expose une
**boucle d'auto-update du noyau** pilotée par un LLM local (**Ollama**) avec
garde-fou de boot.

Tous les scripts sont en **Python**. `/init` lui-même est un programme Python
(via `ctypes` pour `mount`/`finit_module`/loop/`switch_root`) ; aucun shell sur
le chemin de boot normal (busybox n'est embarqué que comme secours).

## Matériel cible

- CPU **Intel i5-11400** (Rocket Lake, 6c/12t, AVX-512 VNNI si activé au BIOS)
- iGPU **UHD 730** (`8086:4c8b`) piloté par **xe** (`force_probe=4c8b`), **i915** en repli
- NIC **Realtek r8169** + **REALTEK_PHY** en dur
- **ZFS** en module hors-arbre (CDDL — jamais `=y`)
- 128 Go DDR4, 2 NVMe en stripe — pas de NPU, l'inférence vise le CPU

## Disposition ZFS

| Élément | Emplacement |
|---|---|
| Noyau `vmlinuz-<ver>` (bzImage) | `fast_pool/boot/` → copié sur l'ESP au déploiement |
| `rootfs.sfs` (Gentoo) | `fast_pool/sfs` |
| `modules-<ver>.sfs` | `fast_pool/sfs` |

Le firmware UEFI ne lit pas ZFS : noyau et initramfs sont **stagés sur l'ESP
(FAT32)** ; `fast_pool` ne sert que de stockage.

---

## Arborescence du projet

```
.
├── README.md
├── init.py               # PID 1 de l'initramfs (ctypes) — installé comme /init
├── session_launch.py     # post-switch_root : seatd + cage + bascule stream wayland
├── build_initramfs.py    # construit initramfs-<ver>.zst (embarque CPython)
├── efi_install.py        # install EFI initiale (un seul noyau)
├── kernel_watch.py       # auto-update 1/3 : moniteur + propositions config (LLM)
├── kernel_build.py       # auto-update 2/3 : compile + zfs-kmod + sfs + EFI + BootNext
└── boot_confirm.py       # auto-update 3/3 : health-check + promotion BootOrder
```

Fichiers déployés **dans le rootfs Gentoo** :

```
/sbin/session_launch.py              # depuis session_launch.py
/usr/local/sbin/boot_confirm.py      # depuis boot_confirm.py
/etc/init.d/stream-session           # service OpenRC
/etc/init.d/boot-confirm             # service OpenRC
```

---

## 1. Système Gentoo — `make.conf`

```sh
# /etc/portage/make.conf
COMMON_FLAGS="-O2 -march=native -pipe"   # native -> AVX-512 VNNI si dispo (gain inférence CPU)
MAKEOPTS="-j12"                          # 6c/12t
VIDEO_CARDS="intel iris"                 # iris = Mesa GL Gen12 ; intel = Vulkan ANV
USE="vaapi wayland vulkan -X"
ACCEPT_KEYWORDS="amd64"
ACCEPT_LICENSE="* -@EULA"                # CDDL (zfs) + firmware redistribuable
```

> `lscpu | grep avx512` pour confirmer l'AVX-512 (BIOS-dépendant sur Rocket Lake).
> Après changement de flags : `emerge -e @world` (ou au moins recompiler `ollama`,
> `mesa`, `ffmpeg`, les modules noyau et `python`).

## 2. Overlay GURU + keywords / USE

```sh
eselect repository enable guru
emaint sync -r guru
```
```sh
# /etc/portage/package.accept_keywords/ai
sci-ml/ollama ~amd64

# /etc/portage/package.use/ai
sci-ml/ollama -cuda                       # Intel : PAS de cuda (évite le bug acct-user)
media-video/ffmpeg vaapi x264 opus vorbis
```

## 3. Paquets

```sh
# Boot / ZFS / EFI / outils initramfs
emerge -av sys-fs/zfs sys-fs/zfs-kmod sys-boot/efibootmgr \
           sys-fs/squashfs-tools app-arch/zstd app-arch/cpio \
           sys-apps/busybox sys-kernel/linux-firmware   # rtl_nic + GuC/HuC (xe)

# clang/LLVM requis par la chaine OpenCL Intel (intel-graphics-compiler ->
# opencl-clang -> llvm-core/clang). Categorie deplacee sys-devel -> llvm-core,
# paquets slottes. Le USE static-analyzer (actif par defaut) est obligatoire
# sur clang, sinon erreurs de linker au build d'opencl-clang :
echo "llvm-core/clang static-analyzer pie extra" >> /etc/portage/package.use/ai
emerge -av llvm-core/clang llvm-core/llvm
# USE=clang reste DESACTIVE par defaut sur profil non-LLVM (GCC continue a
# servir de compilateur systeme) -- seul le binaire clang est requis ici.

# iGPU : OpenCL + Level Zero (NEO) + VAAPI media
emerge -av dev-libs/intel-compute-runtime dev-libs/level-zero \
           media-libs/libva-intel-media-driver media-libs/libva
# le driver media s'appelle iHD ; libva charge l'ancien par defaut sans ca :
echo 'LIBVA_DRIVER_NAME="iHD"' >> /etc/env.d/90intel-media
env-update && source /etc/profile

# Session graphique + capture/stream
emerge -av gui-wm/cage sys-auth/seatd gui-apps/foot \
           gui-apps/wf-recorder media-video/ffmpeg
# wl-screenrec (préféré, VAAPI Intel) : GURU ou `cargo install wl-screenrec`

# Inférence
emerge -av sci-ml/ollama
```

> `python3` (et donc `ctypes`) est déjà fourni par `dev-lang/python` sur Gentoo —
> rien à installer en plus pour les scripts du projet.

## 4. Accès GPU + service Ollama

```sh
usermod -aG render,video <utilisateur>     # /dev/dri (compositeur + compute)

rc-update add ollama default
rc-service ollama start                    # API sur http://127.0.0.1:11434
ollama pull qwen3:30b                       # tag exact à vérifier via `ollama list`
```

## 5. Déploiement des scripts dans le rootfs

À faire dans le rootfs Gentoo **avant** de générer `rootfs.sfs` :

```sh
install -m 0755 session_launch.py /sbin/session_launch.py
install -m 0755 boot_confirm.py   /usr/local/sbin/boot_confirm.py
```

Services OpenRC (le `/init` Python fait `switch_root` vers `/sbin/init` si tu
actives OpenRC — voir §10) :

```sh
# /etc/init.d/stream-session
#!/sbin/openrc-run
command="/usr/bin/python3"
command_args="/sbin/session_launch.py"
command_background="yes"
pidfile="/run/stream-session.pid"
depend() { after udev; }
```
```sh
# /etc/init.d/boot-confirm
#!/sbin/openrc-run
command="/usr/bin/python3"
command_args="/usr/local/sbin/boot_confirm.py"
depend() { after stream-session; }
```
```sh
rc-update add stream-session default
rc-update add boot-confirm  default
```

## 6. Noyau — `.config`

```
# Built-in
CONFIG_EFI=y, CONFIG_EFI_STUB=y
CONFIG_BINFMT_SCRIPT=y                   # exécuter /init via son shebang python
CONFIG_BLK_DEV_NVME=y, CONFIG_SATA_AHCI=y
CONFIG_SQUASHFS=y, CONFIG_SQUASHFS_ZSTD=y, CONFIG_SQUASHFS_XATTR=y
CONFIG_OVERLAY_FS=y, CONFIG_BLK_DEV_LOOP=y
CONFIG_DEVTMPFS=y, CONFIG_DEVTMPFS_MOUNT=y, CONFIG_TMPFS=y
CONFIG_DRM=y, CONFIG_DRM_XE=y, CONFIG_DRM_XE_DISPLAY=y
CONFIG_DRM_XE_FORCE_PROBE="4c8b"
CONFIG_DRM_I915=y                        # repli
CONFIG_FB=y, CONFIG_FRAMEBUFFER_CONSOLE=y, CONFIG_VT=y
CONFIG_R8169=y, CONFIG_REALTEK_PHY=y     # PHY en dur OBLIGATOIRE avec r8169=y
CONFIG_IP_PNP=y                          # réseau configuré avant l'userspace
CONFIG_FW_LOADER=y
CONFIG_RD_ZSTD=y                         # décompression de l'initramfs .zst
# Firmware (rtl_nic + i915 GuC/HuC/DMC) embarqué dans l'initramfs par
# build_initramfs.py -> CONFIG_EXTRA_FIRMWARE inutile (l'initramfs est
# décompressé AVANT les initcalls des drivers =y, donc /lib/firmware est là).

# Modules hors-arbre (chargés par init.py via finit_module)
zfs, spl
```

> `xe` est **GuC-obligatoire** : le firmware GuC/HuC doit être présent quand le
> GPU s'initialise. Ici `build_initramfs.py` les place dans l'initramfs, qui est
> décompressé avant les initcalls — donc rien à embarquer dans le noyau.

Build :
```sh
eselect kernel set linux-<ver>           # /usr/src/linux -> ton arbre
cd /usr/src/linux
make -j"$(nproc)" && make modules_install
emerge -1 sys-fs/zfs-kmod                 # zfs.ko/spl.ko contre ce noyau
```

### Ligne de commande noyau

```
i915.force_probe=!4c8b xe.force_probe=4c8b ip=192.168.1.10::192.168.1.1:255.255.255.0::eth0:off:8.8.8.8 console=tty0 loglevel=4
```

## 7. Générer `rootfs.sfs`

```sh
zfs mount fast_pool/sfs
mksquashfs <racine_gentoo> $(zfs get -H -o value mountpoint fast_pool/sfs)/rootfs.sfs \
           -comp zstd -xattrs -noappend
```
(la racine doit contenir python3, les paquets §3, et les scripts §5)

## 8. Construire l'initramfs

`build_initramfs.py` embarque CPython (interpréteur + stdlib allégée + `.so` via
`ldd`), busybox (secours), `zpool`/`zfs`/`mount.zfs`/`ip`, décompresse
`spl.ko`/`zfs.ko`, copie les firmware (`rtl_nic` + `i915/tgl_*`/`rkl_*` pour
GuC/HuC/DMC — Rocket Lake réutilise les blobs Tiger Lake), crée les nœuds
`/dev`, et installe `init.py` comme `/init`.

> Prérequis : `sys-kernel/linux-firmware` doit être installé sur la machine de
> build (les blobs sont lus depuis `/lib/firmware/`). Les motifs sont
> surchargeables via `FW_GLOBS`.

```sh
# À lancer en root, avec le python SYSTÈME (PAS dans un venv)
sudo /usr/bin/python3 build_initramfs.py     # -> initramfs-<ver>.zst (~30-50 Mo)
```

## 9. Install EFI (initiale)

```sh
# ajuster DISK/PART/KERNEL_SRC via l'environnement si besoin
sudo /usr/bin/python3 efi_install.py
```
Désactiver **Secure Boot** (ou signer le bzImage), puis rebooter.

Chaîne : firmware → bzImage (EFI stub) → `/init` = `init.py`
(zfs, overlay, réseau, stream) → `switch_root` → `session_launch.py`.

## 10. Astuces avant d'installer

### Check rapide du `.config` (avant de builder/déployer)

Une ligne, sans script dédié — vérifie que les options critiques sont bien
posées dans le `.config` qui va servir au build :

```sh
grep -E 'CONFIG_(DRM_XE|DRM_I915|R8169|REALTEK_PHY|IP_PNP|SQUASHFS|SQUASHFS_XATTR|OVERLAY_FS|BLK_DEV_LOOP|EFI_STUB|BINFMT_SCRIPT|RD_ZSTD)=' /usr/src/linux/.config
# ZFS/SPL doivent etre EN MODULE, jamais =y (licence CDDL) :
grep -E 'CONFIG_(ZFS|SPL)=' /usr/src/linux/.config   # attendu : =m
```
Toute ligne absente = option non posée (souvent `# CONFIG_X is not set`).
Pour un noyau **déjà booté** (si `CONFIG_IKCONFIG_PROC=y`), remplace le chemin
par `/proc/config.gz` et préfixe par `zcat`.

### `mksquashfs` avec xattr (ACL, capabilities, contextes SELinux)

Sans xattr, les `setcap`/ACL/contextes SELinux posés dans le rootfs sont
**perdus** à l'empaquetage. Toujours :

```sh
mksquashfs <racine_gentoo> rootfs.sfs   -comp zstd -Xcompression-level 19 -xattrs -noappend
mksquashfs /lib/modules/<ver> modules-<ver>.sfs -comp zstd -xattrs -noappend
```
Nécessite `CONFIG_SQUASHFS_XATTR=y` côté noyau (ajouté au fragment §6) pour que
les xattr soient **lus** au montage — sinon ils sont silencieusement ignorés.

### Contextes SELinux (si tu actives SELinux dans le rootfs)

À faire **avant** `mksquashfs`, sur l'arbre du futur rootfs (overlay en lecture
seule ensuite → impossible de relabel après coup) :

```sh
emerge -av sys-apps/policycoreutils    # fournit setfiles/semanage
setfiles -r <racine_gentoo> \
  /etc/selinux/<SELINUXTYPE>/contexts/files/file_contexts <racine_gentoo>
```
`<SELINUXTYPE>` = `targeted`/`strict`/`mcs` selon `/etc/selinux/config`. Sans
SELinux, ignore cette étape.

### Datasets ZFS — création avec les bonnes options

```sh
# dataset de stockage (rootfs.sfs, modules-*.sfs) : pas de double-compression
zfs create -o compression=off -o atime=off -o mountpoint=/mnt/sfs fast_pool/sfs

# si tu actives la persistance de l'overlay (upper en dataset au lieu de tmpfs) :
zfs create -o compression=zstd -o atime=off -o xattr=sa -o acltype=posixacl \
           -o mountpoint=none fast_pool/overlay
```
- `compression=off` sur `fast_pool/sfs` : les `.sfs` sont déjà compressés
  (zstd) — recompresser coûte du CPU pour rien.
- `compression=zstd` + `xattr=sa` + `acltype=posixacl` sur un dataset
  **overlay persistant** : xattr en SA (plus rapide que les xattr "directory"
  historiques) et ACL POSIX si le rootfs en a besoin.
- `atime=off` partout sur cette appliance : aucun outil n'a besoin des
  `atime`, et ça évite des écritures pour rien (pertinent même en NVMe).

### Autres astuces d'installation / optimisation

- **`zpool import -f -d /dev`** dans `init.py` : le `-d /dev` évite que ZFS
  scanne tous les `/dev/*` (plus rapide, et plus sûr si plusieurs pools
  existent sur la machine pendant les tests).
- **`ashift`** : si tu recrées `fast_pool` un jour, force `-o ashift=12` (NVMe
  4K) à la création du pool — non corrigeable après coup.
- **`mksquashfs -processors $(nproc)`** : par défaut squashfs-tools utilise
  déjà tous les cœurs, mais le préciser évite les surprises sur certains
  builds.
- **Élagage** : `efibootmgr -v` + `ls $ESP/EFI/gentoo/` de temps en temps —
  chaque cycle d'auto-update laisse un `vmlinuz-<ver>.efi` +
  `initramfs-<ver>.zst` + une entrée EFI. Garde au moins le `BootOrder[0]`
  actuel et un fallback connu, supprime le reste.
- **`zfs set relatime=off`** (inclus dans `atime=off` ci-dessus) plutôt que de
  laisser le défaut `relatime` — appliance, pas de besoin de traçabilité d'accès.

---

## 11. Variables à éditer

- `init.py` : `IP_ADDR`, `GATEWAY`, `DNS`, `YT_KEY` — `KVER` dérivé de `uname -r`
- `session_launch.py` : clé lue dans `/etc/yt.key` (posée par `init.py`)
- `build_initramfs.py` : `KVER`, `PYBIN`, `FFMPEG_STATIC` (env)
- `efi_install.py` / `kernel_build.py` : `ESP`, `DISK`, `PART`, `CMDLINE` (env)
- `kernel_watch.py` : `--src`, `--endpoint`, `--model`

---

## Boucle d'auto-update du noyau

```sh
# 1. proposer la config (validation locale interactive)
python3 kernel_watch.py --src /usr/src/linux \
  --endpoint http://127.0.0.1:11434/v1 --model qwen3:30b
#    (--force pour tester sans nouvelle version)

# 2. compiler + stager + armer BootNext (essai unique)
sudo /usr/bin/python3 kernel_build.py

# 3. reboot -> boot du noyau testé -> boot_confirm.py (service OpenRC) :
#      santé OK -> promotion en tête de BootOrder (devient défaut)
#      panic    -> power-cycle : BootNext consommé -> noyau précédent
```

### Garde-fou `BootNext`

Consommé après **un seul** boot : un noyau qui plante n'est jamais promu, et le
power-cycle suivant repart sur `BootOrder[0]` = dernier noyau bon. Aucun code de
revert. Les anciens noyaux/initramfs restent sur l'ESP (élaguer à la main).

---

## Inférence (Ollama)

```sh
ollama serve     # ou le service OpenRC, API sur 11434
ollama pull qwen3:30b
```
Sur ce CPU (RKL, pas de NPU), le MoE **Qwen3-30B-A3B** (≈3B actifs) donne le
meilleur rapport vitesse/qualité ; gros modèle en RAM, peu de compute actif.
`kernel_watch.py` parle à n'importe quel endpoint OpenAI-compatible (`--endpoint`).

### OpenVINO (optionnel, plus tard)

Pas de USE flag transverse dans Portage → venv pip :
```sh
python -m venv /opt/agent-venv
/opt/agent-venv/bin/pip install openvino openvino-genai "optimum[openvino]" transformers nncf
```
Accélération iGPU via `intel-compute-runtime` + `level-zero`, **mais sur Rocket
Lake le device Level-Zero GPU est souvent absent** (`sycl-ls` pour vérifier) → le
CPU reste la cible fiable.

---

## Optimisations (récap)

- **CPU** : `-march=native` → AVX-512 VNNI (si BIOS) pour Ollama/OpenVINO.
- **Modèle** : MoE quantifié (Qwen3-30B-A3B GGUF Q4_K_M).
- **iGPU stream** : `VIDEO_CARDS="intel iris"` + `libva-intel-media-driver` (iHD, `LIBVA_DRIVER_NAME=iHD`) → encode VAAPI (`wl-screenrec`).
- **Initramfs** : stdlib Python allégée (test/idlelib/tkinter exclus), modules décompressés une fois.
- **Groupes** : `render,video` sur l'utilisateur pour `/dev/dri`.

## Notes / limites

- `init.py` est figé sur **x86_64** (`NR_finit_module=313`, loader `ld-linux-x86-64`).
- CPython embarqué gonfle l'initramfs de ~30-50 Mo (coût de « tout en Python »).
- Les appels `mount`/`finit_module`/`switch_root` ne se valident qu'au **boot réel** ;
  le reste (parseurs, liaison ctypes) est testé hors-cible.

## À venir

- Rapporteur **stream → chat YouTube** (Data API v3 : post compile/boot/promotion
  + lecture de commandes gatées owner/modérateurs).
- Extension de l'auto-update au-delà du noyau (set Gentoo ciblé, OpenVINO).

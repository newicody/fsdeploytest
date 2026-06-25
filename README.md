# fsdeploytest — appliance Gentoo : boot ZFS, stream YouTube, inférence locale

Appliance **100% Python** qui démarre en **UEFI-direct (EFI stub, sans ZFSBootMenu)**,
importe les pools ZFS, monte le rootfs Gentoo en **squashfs + overlay**, diffuse le
framebuffer puis la session Wayland sur **YouTube**, et pilote des boucles
d'**inférence locale** (OpenVINO / Ollama) — mise à jour des noyaux et **couche
GitOps multi-dépôts** comprises.

> `infra.conf` est la **source de vérité unique**. Toute modification est un splice
> Python chirurgical (jamais `configobj.write()`, qui reformaterait tout le fichier).

---

## 1. Matériel

- CPU **i5-11400** (6c/12t), iGPU **UHD 730** (`xe.force_probe=4c8b`, repli `i915`).
- NIC **Realtek r8169**. 128 Go DDR4. 2× NVMe (stripe `fast_pool`).
- **ZFS out-of-tree** (`zfs-kmod`). Inférence **CPU**.

## 2. Pools & datasets

| Pool | Type | Rôle | Datasets clés |
|------|------|------|---------------|
| `fast_pool` | stripe (volatile, reconstructible) | images actives, build | `sfs` (rootfs-vN.sfs + modules), `staging`, `usr-src` (sources noyau), `rootfs`, `log`, `tmp`, `var`, `reserve` |
| `boot_pool` | mirror (durable) | secours + état | `images` (rescue sfs), `efi-backup`, `manager` (config + état + registre) |
| `data_pool` | raidz2 (précieux) | données | `home` (`/home`), `modeles` (`/var/lib/models`), `log`, `archives` |

`[datasets]` dans `infra.conf` déclare propriétés ZFS, rôle, `canmount`, `mountpoint`,
`compression` — lu par `first_boot` (création) et `storage_manager`/`operate` (vérif).

## 3. Flux de boot (réel)

```
UEFI ─▶ EFI stub : UKI (noyau + initramfs + cmdline)        [profils dans [uki]]
  │
  ├▶ init.py        PID 1 de l'INITRAMFS (Python via ctypes, pas de shell)
  │     • réseau statique depuis la cmdline ip=…             (très tôt)
  │     • import ZFS   ── cachefile embarqué ⇒ instantané (sinon scan -d, lent)
  │     • stream fbdev /dev/fb0 ─▶ YouTube   ── si 'stream' présent dans la cmdline
  │     • rootfs = squashfs + overlay (upper sur fast_pool)
  │     • switch_root réimplémenté (MS_MOVE + chroot + execv)
  │
  └▶ session_launch.py   PID 1 du ROOTFS
        • OpenRC bringup ; /run/initctl créé+drainé ; MOISSONNAGE des zombies
        • zfs mount -a (contrat canmount) ; /home en overlay
        • services : dbus (système), udev, chronyd, sshd, …
        • session : bash --login -c 'dbus-run-session sway'  (utilisateur dédié,
          os.initgroups ; sway lit ~/.config/sway — Python ne gère PAS la session)
        • garde anti-panic _never_die : PID 1 ne meurt JAMAIS → shell de secours
```

Le stream est en **deux temps** : `init` capture la console de boot via **fbdev sur
`/dev/fb0`** (fourni tôt par `simpledrm`, le plus stable/léger — pas de kmsgrab), puis
la session bascule sur une capture Wayland. Socle noyau requis : `CONFIG_FB`,
`DRM_SIMPLEDRM`, `DRM_FBDEV_EMULATION`, `FB_EFI` (vérifiés par `kernel_diagnose`).

## 4. Les 3 points d'entrée

### `first_boot.py` — bootstrap / déploiement (chroot)
Crée les datasets, **pose la source noyau** (`source_manager.ensure` → fetch tarball +
select `/usr/src/linux`), stage le `.config`, puis **délègue à `kernel_build`**
(compile → `zfs-kmod` → `modules.sfs` → `initramfs` → ESP → entrée EFI) et enregistre
au manager. Rapport d'étapes consolidé (empreinte de conformité + build).

### `operate.py` — dispatcher CLI (runtime)
- **Natives** : `status`, `check`, `kernel [--config F] [--expect VER]`, `initramfs`,
  `esp`, `replicate`, `confirm`.
- **Déléguées** (PASS → module, `INFRA_CONF` propagé) :
  `config`→kernel_watch · **`source`→source_manager** · **`dispatch`→GitOps** ·
  `diagnose`→kernel_diagnose · `rootfs`→sfs_build · `select`/`clean`→rootfs ·
  `freeze`→freeze_overlay · `snapshot`→snapshot_manager · `storage`→storage_manager ·
  `validate`→validate_boot · `bench`→machine_bench · `brainstorm` · `rag`.

### `manager` = `kernel_registry.py` (sur `boot_pool/manager`)
Registre des **noyaux + sources + historique** (`log_event` : compile / study /
promote / source-fetch / source-select). État durable, anti-corruption (.bak, écriture
atomique). Push Git via `manager_git`.

## 5. Ligne noyau & sources (de bout en bout)

```
source_manager (usr-src)              kernel_watch         kernel_build              kernel_registry
 fetch tarball kernel.org  ─▶ select   .config + olddef  ─▶ build SRC (KVER_EXPECT)  ─▶ trace + promote
 /usr/src/linux ─▶ linux-<ver>                            modules.sfs / initramfs / EFI
```

- **`source_manager`** possède le cycle de vie des sources (le « repoint
  `/usr/src/linux` ») : `operate source list|fetch [VER] [--select]|select VER|remove`.
- **`kernel_build`** dérive la version du source (`make kernelrelease`) ; le garde-fou
  **`KVER_EXPECT`** refuse, avant toute compilation, si le source ≠ version demandée
  (posé par `operate kernel --expect` et par `mode_kernel` via la validation GitOps).
- **`kernel_diagnose`** : cohérence de `.config` (CRIT/WARN), dont le socle stream.

## 6. Couche GitOps / artefacts (multi-dépôts)

Registre `[projects]` : N dépôts × **mode**. Les **Issues GitHub sont les artefacts**
(labels `type:` `state:` `axis:` `domain:` `route:` `needs:`). Aucune infra parallèle :
tout réutilise `github_board`.

```
push / photo / question
   └▶ Action Copilot (CI du dépôt)  ── 1er travail : réfléchit, analyse l'image,
        oriente, croise les dépôts ─▶ Issue labellisée (artefact)
            └▶ projects  (ingestion + classify)
                 └▶ dispatch   route:direct ─▶ handler mode
                              │ needs:inference ─▶ arbiter
                              └ (défaut) ─▶ handler mode
                                   arbiter : workers // (Claude · Gemini · local OpenVINO)
                                             ─▶ OpenVINO ARBITRE (tri + synthèse)
                 └▶ post_feedback  ── commentaire + label machine:<action>
                                      (NE TOUCHE PAS au state: — c'est ton kanban)
   ◀── tu lis le soir, push parent, le state: évolue : idea ▸ explore ▸ dev ▸ wip ▸ prod
                                       (+ hold, drop ; explore = machine a traité, à toi)
```

- `taxonomy.conf` déclare **axes** (inventaire/veille/operations/etude/ressources/
  validation/diffusion), **states**, **modes** + **domaines** ; chargé par `taxonomy.py`,
  consommé par `projects.classify` (validation permissive). Seule transition machine :
  `idea→explore`.
- `mode_<x>.py` = handler par mode enregistré dans `dispatch`. `mode_kernel` :
  `type:kernel-validation` + `state:prod` → build gardé (dry-run sauf `--apply`).
- `arbiter` (`[arbiter]`) : workers `local`/`claude`/`gemini`/`stub` ; clés en ENV
  (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) ; modèle OpenVINO local = worker local **et**
  arbitre ; sans modèle → arbitrage par fusion. Robuste (worker en échec ignoré).

Détail des modes métiers (garage / kernel / ideas / science) et de leurs axes :
voir **`modes.md`**.

## 7. `infra.conf` — sections clés

`[datasets]` · `[kernel]` (src / version / mirror / jobs / make_flags / cmdline) ·
`[uki]` (profils EFI, dont le profil stream) · `[efi]` · `[session]`
(user / groups / session_cmd) · `[projects]` · `[arbiter]` · `[manager]` · `[git]` ·
`[services]` · `[firmware]` · `[snapshots]` · `[replication]`.

## 8. Carte des modules

| Domaine | Modules |
|---------|---------|
| Boot / initramfs | `init.py`, `build_initramfs.py`, `initramfs_verify.py`, `uki_build.py`, `boot_layout.py`, `boot_confirm.py`, `validate_boot.py` |
| Session (rootfs PID 1) | `session_launch.py` |
| Points d'entrée | `first_boot.py`, `operate.py`, `kernel_registry.py` (manager) |
| Noyau & sources | `source_manager.py`, `kernel_build.py`, `kernel_watch.py`, `kernel_diagnose.py`, `config_delta.py`, `config_history.py` |
| Rootfs / SFS | `sfs_build.py`, `clean_rootfs.py`, `select_rootfs.py`, `freeze_overlay.py` |
| ZFS / stockage | `zfs_mounts.py`, `zfs_replicate.py`, `storage_manager.py`, `snapshot_manager.py` |
| GitOps / artefacts | `projects.py`, `dispatch.py`, `mode_kernel.py`, `taxonomy.py` (+`taxonomy.conf`), `github_board.py`, `github_api.py`, `github_project.py`, `manager_git.py` |
| Inférence | `arbiter.py`, `ov_pipelines.py` |
| Outils IA | `brainstorm.py`, `rag.py`, `machine_bench.py` |
| Commun / tests | `common.py`, `test_github.py`, `test_project.py` |

## 9. Conventions

- **100% Python**, ASCII strict dans les `.py` (échappements `\uXXXX` si besoin).
- `infra.conf` : splices Python chirurgicaux, jamais `.write()`.
- Pas d'infra parallèle : on branche sur l'existant (`github_board`, `zfs_mounts`,
  `ov_pipelines`, `kernel_build`…).
- PID 1 (init, session_launch) : ne meurt jamais (sinon kernel panic) ; moissonne les
  zombies ; phases isolées.
- `mount -t zfs` canonique ; `-o zfsutil` pour mountpoint non-legacy ; `zfs mount -a`
  respecte `canmount` (ne pas forcer dataset par dataset).

---

## Roadmap

### Court terme — rendre la boucle complète opérante
- [ ] **Action Copilot par dépôt** (CI) : 1er travail (réflexion sur push/question,
      analyse d'image, orientation, croisement inter-dépôts) → Issue labellisée. Hors
      codebase machine ; un dépôt = un mode.
- [ ] **`mode_garage`** + **worker VLM** dans `arbiter` (`ov_pipelines` a `VLMPipeline`)
      + récupération de l'image attachée à l'Issue → l'arbitre *voit* la photo.
- [ ] Provisionner le **modèle OpenVINO** (worker local + arbitre) ; renseigner
      `[arbiter] model`, `workers = local, claude, gemini` ; clés en ENV.
- [ ] Affiner **`taxonomy.conf`** (domaines garage/science détaillés de `modes.md`).

### Validation matérielle (à faire sur la machine)
- [ ] **Stream** : activer `CONFIG_FB`/`DRM_SIMPLEDRM`/`DRM_FBDEV_EMULATION`/`FB_EFI`,
      recompiler, vérifier `ls /dev/fb0` au boot avec le profil stream (token `stream`
      déjà dans la cmdline).
- [ ] **Import ZFS rapide** : `zpool set cachefile=/etc/zfs/zpool.cache fast_pool
      boot_pool data_pool` puis rebuild initramfs (le cache y est embarqué).
- [ ] **Session/D-Bus** : dans `~/.config/sway` ajouter
      `exec dbus-update-activation-environment --all`, puis `exec pipewire` /
      `pipewire-pulse` / `wireplumber` (dans cet ordre). Vérifier : plus de `defunct`,
      `busctl --user` répond, `wpctl status`.
- [ ] Audio : si wireplumber râle encore → **elogind** en service (ou désactiver son
      module logind).
- [ ] Régler `[manager]` (git_remote + token_file) ; pousser le code de l'appli.

### Moyen terme — étendre les modes
- [ ] **`mode_science`** (CFD / maillages / fluides / méca / FEA / PCB-KiCad / recherche
      évolutionnaire) ; **`mode_ideas`** (ingestion bas-seuil → promotion).
- [ ] Mise en relation **inter-dépôts** (un calcul FEA demandé par le garage délégué à
      `science`).
- [ ] Boucle de mise à jour noyau pilotée : veille (suivi sources/bugs/releases) →
      `mode_kernel` → build gardé → `post_feedback`.

### Plus tard
- [ ] Self-update du **code de l'appli** sur dataset (différé : périmètre actuel =
      sources noyau seulement).
- [ ] Capture Wayland fine de la session (`wl-screenrec`, protocole screencopy).

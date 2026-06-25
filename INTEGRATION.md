# Intégration — inférence autonome + accès maintenance fichiers/noyau

Deux objectifs, **sans toucher au nombre de portes d'entrée** (`first_boot.py`,
`operate.py`, `kernel_registry.py`) :

1. **Inférence autonome** — `dispatch_service.py` (livré séparément) tourne en
   tâche de fond, piloté *seulement* par `infra.conf`. Aucune commande.
2. **Maintenance noyau accessible** — `config_history` reçoit une CLI ;
   `config-delta` + le manager sont câblés dans `operate`.

Chaque patch est chirurgical (splice ciblé), conforme aux conventions du projet
(ASCII strict dans les `.py`, pas de réécriture massive).

---

## 1. `dispatch_service.py` — déposer le nouveau fichier

Copier `dispatch_service.py` à la racine du dépôt (à côté de `dispatch.py`).
Il n'a **aucun** point d'entrée `operate` : il est lancé par `session_launch`
(étape 4) et lit `[dispatch]` d'`infra.conf` (étape 3). `enabled=false` par
défaut → inactif (sûr en chroot et tant que tu ne l'actives pas).

---

## 2. `config_history.py` — APPENDER ce bloc en fin de fichier

`config_history` est aujourd'hui une bibliothèque sans CLI (la collecte se fait
dans `kernel_watch` via `record()`). On lui ajoute une CLI de **consultation /
rendu à la demande** — la seule chose qui manquait pour la maintenance noyau.
Coller tel quel **à la fin** du fichier :

```python
# ===========================================================================
# CLI : consultation / rendu de l'historique a la demande (maintenance noyau).
# La COLLECTE reste faite par kernel_watch via record() ; ici on REND (svg+index)
# et on INSPECTE (list/show) sans recompiler. Appele par 'operate config-history'.
# ===========================================================================
def _default_hist():
    import os
    return os.path.join(os.environ.get("MANAGER_ROOT", "/boot_pool/manager"),
                        "config-history")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="historique des configs noyau (rendu + consultation)")
    ap.add_argument("--hist", default=None,
                    help="dossier d'historique (defaut <MANAGER_ROOT>/config-history)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("render")
    ps = sub.add_parser("show")
    ps.add_argument("kver")
    a = ap.parse_args()
    hist = a.hist or _default_hist()
    base = Path(hist)

    if a.cmd == "list":
        if not base.is_dir():
            print(f"(aucun historique sous {hist})")
            return 0
        rows = sorted(d.name for d in base.iterdir()
                      if d.is_dir() and (d / "delta.json").exists())
        if not rows:
            print(f"(aucune config archivee sous {hist})")
            return 0
        for kv in rows:
            try:
                delta = json.loads((base / kv / "delta.json").read_text())
                n = sum(len(delta.get(c, [])) for c in CATS)
            except Exception:
                n = "?"
            print(f"  {kv:18} {n} changement(s)")
        return 0

    if a.cmd == "render":
        try:
            svg = render_graph(hist)
            idx = render_index(hist)
            print(f"graphe : {svg}")
            print(f"index  : {idx}")
            return 0
        except Exception as e:
            print(f"!! rendu impossible ({e})")
            return 1

    if a.cmd == "show":
        doc = base / a.kver / "doc.md"
        if doc.exists():
            print(doc.read_text())
            return 0
        print(f"(pas de doc.md pour {a.kver} sous {hist})")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main() or 0)
```

`Path`, `json`, `CATS`, `render_graph`, `render_index` sont déjà au niveau module
de `config_history.py` → aucun import à ajouter en tête.

---

## 3. `operate.py` — 3 splices

### 3a. Table `PASS` (remplacer le dict existant)

`dispatch` **disparaît** (l'inférence n'a plus de commande). On ajoute la
maintenance noyau + le manager.

```python
# sous-commandes en simple passthrough vers leur module (memes args/CLI)
PASS = {
    # --- noyau ---
    "config":         "kernel_watch.py",
    "source":         "source_manager.py",
    "diagnose":       "kernel_diagnose.py",
    "config-history": "config_history.py",   # rendu/consultation de l'historique
    "config-delta":   "config_delta.py",     # compare deux .config
    # --- rootfs / sfs / overlay ---
    "freeze":     "freeze_overlay.py",
    "select":     "select_rootfs.py",
    "clean":      "clean_rootfs.py",
    "rootfs":     "sfs_build.py",
    # --- zfs / stockage ---
    "snapshot":   "snapshot_manager.py",
    "storage":    "storage_manager.py",
    "validate":   "validate_boot.py",
    # --- manager (registre noyaux + audit trail git) ---
    "manager":      "kernel_registry.py",    # list/audit/history/mark/promote
    "manager-sync": "manager_git.py",        # synchro git du audit trail
    # --- outils ---
    "bench":      "machine_bench.py",
    "brainstorm": "brainstorm.py",
    "rag":        "rag.py",
}
# NB: 'dispatch' (boucle GitOps/inference) N'EST PLUS une commande : l'inference
# est AUTONOME (dispatch_service.py, pilote par [dispatch] d'infra.conf, lance par
# session_launch). Pour debugger a la main, hors operate : 'python3 dispatch.py'.
```

### 3b. `NO_ROOT` (les commandes de maintenance lecture/rendu ne touchent pas au système)

```python
NO_ROOT = {"status", "rag", "brainstorm", "check",
           "config-history", "config-delta", "manager", "manager-sync"}
```

### 3c. `USAGE` (remplacer la ligne `commandes :` par un récap groupé)

```python
USAGE = (
    "usage: operate.py [--infra PATH] <commande> [args...]\n"
    "\n"
    "commandes :\n"
    "  noyau    : kernel source config diagnose config-history config-delta\n"
    "  rootfs   : rootfs select clean freeze\n"
    "  stockage : snapshot storage replicate validate\n"
    "  manager  : manager manager-sync\n"
    "  systeme  : status check initramfs esp confirm bench brainstorm rag\n"
    "\n"
    "Utilisable en chroot / booted / rescue (lit l'infra.conf de la machine).\n"
    "Tout ce qui suit <commande> est transmis au module cible (ex:\n"
    " 'operate.py kernel --config K.config -j8', 'operate.py manager audit',\n"
    " 'operate.py config-history list', 'operate.py config-delta A B').\n"
    "L'inference est AUTONOME (service de fond, [dispatch] d'infra.conf) : pas de\n"
    "commande ici. Detail des roles : voir l'entete de ce fichier."
)
```

> Rien d'autre à changer dans `operate.py` : le dispatch natif/PASS, la
> journalisation, la remontée git de fin de commande restent identiques.

---

## 4. `infra.conf` — AJOUTER la section `[dispatch]`

À placer près de `[arbiter]` (même domaine : l'inférence). Splice manuel
(jamais `configobj.write()`), conforme à la règle du projet.

```ini
[dispatch]
# Boucle d'inference AUTONOME (dispatch_service.py), lancee par session_launch en
# arriere-plan. PAS de commande operate : tout se pilote ICI. La boucle interroge
# [projects] (board), route via dispatch (mode_<x> + arbiter si needs:inference)
# et reposte la decision (machine:<action>). Le MODELE est gere par [arbiter].
enabled  = false   # MAITRE : false -> service inactif. Garde false EN CHROOT.
interval = 900     # secondes entre deux passes (900 = 15 min)
feedback = true    # reposter la decision sur l'artefact (label machine:<action>)
apply    = false   # false = dry-run (decision seule) ; true = execute (build...)
jitter   = 60      # alea +/- (s) pour ne pas marteler l'API a heure fixe
```

Quand tu es prêt à rendre la machine réellement autonome sur le board (système
booté, token en place, modèle OpenVINO provisionné dans `[arbiter] model`) :
`enabled = true`, puis `apply = true` une fois la confiance acquise.

---

## 5. `session_launch.py` — lancer le service en tâche de fond

Insérer ce bloc **juste après** l'appel existant
`_safe("run_boot_confirm", run_boot_confirm)` (réseau + token déjà prêts à ce
stade) :

```python
    # INFERENCE AUTONOME : boucle de dispatch GitOps pilotee par [dispatch]
    # d'infra.conf (enabled/interval). En arriere-plan -> ne bloque JAMAIS PID 1.
    # AUCUNE commande : service de fond, pas un point d'entree. Si enabled=false
    # (defaut, et toujours en chroot), serve() rend la main aussitot et le fils se
    # termine (moissonne par PID 1).
    if os.fork() == 0:
        try:
            import dispatch_service
            dispatch_service.serve()
        except Exception as e:
            log(f"[dispatch-service] non demarre ({e})")
        os._exit(0)
```

`session_launch` est déjà PID 1 et moissonne les zombies : un fils qui boucle
(ou se termine immédiatement si `enabled=false`) est géré proprement.

---

## 6. Doc — delta `README.md` (optionnel mais recommandé)

Dans « Carte des modules », ligne *Inférence* :

```
| Inférence (autonome) | `arbiter.py`, `ov_pipelines.py`, `dispatch_service.py` |
```

Dans la section `operate.py` (les commandes déléguées), retirer `dispatch`,
ajouter `config-history` · `config-delta` · `manager`→kernel_registry ·
`manager-sync`→manager_git, et noter : *l'inférence est désormais autonome
(`[dispatch]`), plus une commande.*

---

## Récapitulatif des accès après intégration

| Besoin | Avant | Après |
|---|---|---|
| Inférence GitOps | `operate dispatch` (manuel) | **autonome**, `[dispatch]` d'infra.conf, aucune commande |
| Historique config noyau | inaccessible (lib sans CLI) | `operate config-history list\|render\|show` |
| Comparer deux `.config` | CLI non câblée | `operate config-delta A B` |
| Registre noyaux (manager) | 3e porte uniquement | aussi `operate manager list\|audit\|history\|promote\|mark` |
| Synchro git audit trail | interne seulement | `operate manager-sync` |

Portes d'entrée : toujours **3** (`first_boot.py`, `operate.py`,
`kernel_registry.py`). `dispatch_service.py` est un composant de boot (comme
`init.py` / `session_launch.py`), pas une porte.

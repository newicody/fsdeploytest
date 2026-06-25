# Modes métiers — taxonomie

Organisation des modes projet de l'appliance. Chaque **mode** = une entrée du
registre `[projects]` + un handler `mode_<x>.py` ; chaque artefact (Issue GitHub)
est étiqueté pour être routé et traité finement.

---

## 1. Métastructure (commune à tous les modes)

Plutôt que des arborescences ad hoc, chaque mode se décrit selon **une entité
gérée** et **sept axes transversaux**. Cela rend la taxonomie régulière,
comparable d'un mode à l'autre, et directement traduisible en labels.

### Entité
Ce que le mode gère et autour de quoi tout s'articule :

| Mode | Entité |
|------|--------|
| Garage | un **véhicule** (flotte de véhicules) |
| Kernel | un **noyau** + sa configuration |
| Idées | une **idée / question** (perso + pro confondus) |
| Science | un **phénomène / modèle** |

### Les 7 axes transversaux
Tout sous-domaine d'un mode se rattache à l'un de ces axes :

1. **Inventaire** — les actifs et leur identité (entités, machines, locaux, modèles).
2. **Veille** — acquisition d'information (sources, docs, releases, scraping, histoire).
3. **Opérations** — le travail réel : à faire, en cours, fait, prévisionnel, historique.
4. **Étude** — analyse et modélisation : calcul, simulation, FEA/CFD, forces, statistiques.
5. **Ressources** — les moyens : budget, pièces, outillage, consommables, fluides, énergie.
6. **Validation** — gouvernance : cohérence, validation via projet, cycle kanban.
7. **Diffusion** — sortie et visibilité : temps réel, média, streaming, drones de diffusion.

### Traduction en labels
Un artefact porte (en plus de `type:` / `state:` / `route:` / `needs:` déjà en place) :

```
mode:<garage|kernel|ideas|science|...>      # = entrée du registre
axis:<inventaire|veille|operations|etude|ressources|validation|diffusion>
domain:<sous-domaine precis du mode>        # ex domain:reparation, domain:fea
```

Le `state:` (idea → wip → dev → prod → drop) reste **ton** kanban ; la machine
n'y touche pas (elle ajoute `machine:<action>`).

---

## 2. Mode `garage` — entité : véhicule

### Inventaire
- **Véhicule / identité** : modèle, déclinaison, fabricant, motorisation, châssis,
  options, vendeur, garage d'origine, dates (acquisition, mise en circulation).
- **Véhicule / statut légal et historique** : homologation, assurance, accidents,
  modifications, reprogrammation (cartographie moteur).
- **Outillage** : caisse à outils, outils à main, outils motorisés, machines,
  pneumatiques (compresseur/outils air), locaux/atelier.
- **Drones** : inspection, mesure stationnaire active, diffusion.
- **Atelier / énergie** : énergie, éclairage atelier, swap électrique (poste/baie).

### Veille
- **Documentation véhicule** (pièces, moteur, châssis…) : officielle (constructeur),
  scraping (forums/marchés), livres/manuels, histoire du modèle, études tierces.

### Opérations
- **Réparation** : à faire, effectuées, pièces remplacées.
- **Entretien** : prévisionnel, tâches, historique ; postes : pneus, courroie,
  motorisation, habitacle, nettoyage.

### Étude
- **Domaines techniques** : électricité, carrosserie, mécanique, pièces.
- **Modélisation / simulation** : modélisation 3D, flux (air/fluides), forces,
  poids, transferts (de masse/charge), équilibre.

### Ressources
- **Budget** : coût à l'achat, cote, marché, pièces, devis, factures, réceptions,
  commandes.
- **Consommables** : fluides ; vinyles, colles, solvants, peintures (après-),
  rénovateurs, traitements ; plastiques ; papier ; vis ; papier abrasif ;
  équipement de sécurité.

### Diffusion
- **Exploitation** : logiciel de gestion et de visualisation des données.
- **Diffusion** : drone de diffusion, captation/streaming de l'atelier.

> Exemple de flux : push d'une **photo + question** → Copilot oriente/analyse →
> artefact `mode:garage axis:operations domain:reparation type:question
> needs:inference` → la machine fait travailler plusieurs IA, OpenVINO arbitre,
> retour posté. Le soir tu lis, tu pousses un parent, le `state:` évolue.

---

## 3. Mode `kernel` — entité : noyau + configuration

### Veille (permanente)
- Suivi des **sources**, des **releases**, des **versions**.
- Suivi des **bugs** et de leurs **corrections**.
- Suivi des **mailing lists**, des **fonctionnalités**.
- Veille de **nouveautés** / axes de recherche.

### Étude
- **Analyse d'architecture**.
- **Analyse de l'activité des développeurs** (qui pousse quoi, tendances).
- **Conseil d'implémentation** (propositions guidées).

### Validation
- **Contrôle de cohérence de configuration**.
- **Validation des changements de configuration**.
- **Validation d'action via le projet** (le `state:` autorise build/promote —
  déjà câblé dans `mode_kernel.py`).

### Opérations
- **Tests de performance**, **beta testing**, tests de versioning.

### Diffusion
- **Diffusion temps réel** des efforts de **compilation** et de **démarrage**.
- **Visibilité** des efforts de développement, **médiatisation**.

---

## 4. Mode `ideas` — gestionnaire d'idées global

Transversal, **perso + pro confondus**. C'est le mode d'**ingestion universelle**
qui alimente les autres : capter vite, classer, mettre en relation, promouvoir.

### Opérations
- **Capture** : idée, question, note, lien, image — sans friction.
- **Triage / classification** : attribuer `mode:` / `axis:` / `domain:` cible.

### Étude
- **Mise en relation inter-projets** (rapprocher des artefacts de dépôts différents).
- **Proposition d'axes de recherche**.

### Validation
- **Promotion** : quand une idée devient un système → la pousser vers un mode dédié
  (création/clonage d'un projet) ; sinon elle vit en `state:idea`.

> Ce mode est le point d'entrée « bas seuil » : tout push qui n'a pas encore de
> mode précis y atterrit, puis migre.

---

## 5. Mode `science` — scientisme déterministe

Entité : un **phénomène** et son **modèle**. Étude lourde, orientée calcul.

### Étude — méthodes
- Déterministe ; **évolutionnaire** ; **stochastique** ; **Monte-Carlo** ;
  intégration de **logiques de calcul** (chaînage de solveurs).

### Étude — domaines physiques
- **Thermique** (étude de chaleur).
- **Fluides** (CFD).
- **Mécanique des solides** : déformation, **résistance des matériaux**, **FEA**,
  forces, poussées.
- **Électricité / électromagnétisme**, **physique** générale.

### Inventaire / Ressources
- **Modèles** : maillages, modèles KiCad (PCB), géométries, jeux de paramètres.
- **Outillage de calcul** : solveurs, ressources de calcul (lien avec l'inférence
  locale / dispatch).

### Diffusion
- Restitution des résultats (rapports, visualisations, comparatifs de runs).

> Recoupe le **garage/Étude** (flux, forces, transferts) : un calcul FEA/CFD
> demandé par le garage peut être délégué au mode `science` (mise en relation
> inter-modes).

---

## 6. Mode(s) à venir

Slots futurs (PCB/KiCad autonome, recherche évolutionnaire dédiée, etc.). Tout
nouveau mode = une entrée `[projects]` + un `mode_<x>.py`. Les axes transversaux
restent les mêmes ; seuls les `domain:` changent.

---

## 7. Connexion au système

| Élément | Rôle |
|---------|------|
| `[projects]` (infra.conf) | déclare les dépôts + leur `mode` |
| Action Copilot (CI, par dépôt) | réfléchit au push, analyse image, oriente, croise les dépôts → produit l'artefact étiqueté `mode:/axis:/domain:/type:` |
| `projects.py` | ingestion : Issue → artefact normalisé (lit les labels) |
| `dispatch.py` | route selon `route:` / `needs:` / `mode` |
| `mode_<x>.py` | handler par mode ; lit `axis:`/`domain:` pour un traitement fin |
| OpenVINO (à venir) | fait travailler plusieurs IA, **arbitre**, synthétise |
| `post_feedback` | reposte le résultat → boucle de rétro-action |

### Convention de labels (récapitulatif)
```
mode:<...>      # garage | kernel | ideas | science | ...
axis:<...>      # inventaire | veille | operations | etude | ressources | validation | diffusion
domain:<...>    # sous-domaine precis (reparation, fea, veille-bug, budget, ...)
type:<...>      # kernel-validation | idea | question | report | image-analysis | ...
state:<...>     # idea | wip | dev | prod | drop   (kanban HUMAIN)
route:direct    # bypass orchestrateur
needs:inference # demande l'inference locale
machine:<...>   # statut MACHINE (pose par post_feedback)
```

Cette grille rend chaque artefact **précisément adressable** : un même dépôt peut
mélanger des artefacts de plusieurs axes/domaines, le dispatcher et le handler du
mode savent quoi en faire, et l'arbitre OpenVINO sait quel type d'inférence lancer.

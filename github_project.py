#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
github_project.py — couche GitHub Projects v2 (GraphQL) pour le board.

Mode Projet "pur" : les idees sont des ITEMS de Project (draft items, ou Issues
ajoutees au Project). Le statut = un champ single-select du Project (la COLONNE :
Idea/WIP/Dev/Prod/Drop).

Declencheur double :
  - la COLONNE (champ Status) declenche TOUJOURS (mode projet) ;
  - le LABEL state:* declenche EN PLUS, uniquement si l'item est adosse a une
    Issue (les draft items n'ont pas de labels -> colonne seule).

ATTENTION : l'API Projects v2 est en GraphQL (pas le REST des Issues). Les
requetes ci-dessous sont ecrites au plus pres du schema connu mais DOIVENT etre
validees contre le vrai GitHub (cf. test_project.py) ; un nom de champ peut
devoir etre ajuste au premier essai.

Token : scope 'project' (classic) ou Projects read/write (fine-grained).
Transport interchangeable : ProjectV2Transport (reel) / StubProjectV2 (memoire).
Stdlib uniquement. Generateurs pour l'iteration des items.
"""
import json
import os
import urllib.error
import urllib.request

import brainstorm
from github_api import (ProjectV2Transport as _ApiTransport,  # noqa: F401
                        normalize_item as _api_normalize, BoardAsk)

# nos statuts -> noms d'options de la colonne "Status" du Project (a creer cote
# GitHub avec EXACTEMENT ces noms, ou ajuster ce mapping).
STATUS_OPTION = {
    brainstorm.S_IDEA: "Idea",
    brainstorm.S_WIP:  "WIP",
    brainstorm.S_DEV:  "Dev",
    brainstorm.S_PROD: "Prod",
    brainstorm.S_DROP: "Drop",
}
OPTION_STATUS = {v.lower(): k for k, v in STATUS_OPTION.items()}
STATUS_FIELD_NAME = "Status"        # nom du champ single-select cote Project

# labels (pour le declencheur 'label' quand l'item est une Issue) : reutilise
# le mapping du module Issues si present, sinon le reconstruit.
try:
    import github_board as _gb
    STATE_LABELS = _gb.STATE_LABELS
    LABEL_TO_STATE = _gb.LABEL_TO_STATE
except Exception:
    STATE_LABELS = {k: f"state:{k}" for k in STATUS_OPTION}
    LABEL_TO_STATE = {v: k for k, v in STATE_LABELS.items()}


# --------------------------------------------------------------------------- #
# Les requetes/mutations GraphQL et le client reseau vivent desormais dans
# github_api.py (stdlib, embarquable dans l'initramfs). On les reutilise ici via
# la sous-classe ProjectV2Transport ci-dessous.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class ProjectTransport:
    name = "base"

    def project_id(self, owner, number): raise NotImplementedError
    def status_field(self, project_id): raise NotImplementedError
    def iter_items(self, project_id): raise NotImplementedError
    def add_draft(self, project_id, title, body): raise NotImplementedError
    def set_status(self, project_id, item_id, field_id, option_id):
        raise NotImplementedError


class ProjectV2Transport(_ApiTransport):
    """Transport projet adapte au vocabulaire brainstorm : le RESEAU est herite
    de github_api.ProjectV2Transport (source unique) ; on n'ajoute ici que le
    mapping colonne<->statut (champ 'Status')."""
    name = "graphql"

    def status_field(self, project_id):
        return self.single_select_field(project_id, STATUS_FIELD_NAME)

    def iter_items(self, project_id):
        # github_api rend les NODES bruts ; on applique NOTRE normalisation
        # (colonne -> statut brainstorm), inchangee.
        for node in self.raw_items(project_id, STATUS_FIELD_NAME):
            yield _normalize_item(node)

    def set_status(self, project_id, item_id, field_id, option_id):
        return self.set_field(project_id, item_id, field_id, option_id)


class StubProjectV2(ProjectTransport):
    """Projet simule en memoire : teste toute la logique sans GraphQL reel."""
    name = "stub"

    def __init__(self):
        self._pid = "PRJ_stub"
        self._field = "FLD_status"
        self._opts = {v.lower(): f"OPT_{v}" for v in STATUS_OPTION.values()}
        self._items = {}      # item_id -> {status, content}
        self._n = 0

    def project_id(self, owner, number):
        return self._pid

    def status_field(self, project_id):
        return self._field, dict(self._opts)

    def iter_items(self, project_id):
        for iid, it in self._items.items():
            node = {"id": iid,
                    "fieldValueByName": ({"name": STATUS_OPTION.get(it["status"],
                                          "Idea")} if it["status"] else None),
                    "content": it["content"]}
            yield _normalize_item(node)

    def add_draft(self, project_id, title, body):
        self._n += 1
        iid = f"ITEM_{self._n}"
        self._items[iid] = {"status": brainstorm.S_IDEA,
                            "content": {"title": title}}   # draft = pas de labels
        return iid

    def set_status(self, project_id, item_id, field_id, option_id):
        # retrouver le statut depuis l'option_id
        rev = {v: k for k, v in self._opts.items()}
        opt_name = rev.get(option_id, "idea")
        self._items[item_id]["status"] = OPTION_STATUS.get(opt_name,
                                                           brainstorm.S_IDEA)
        return True

    # aides aux tests : simuler les actions humaines cote GitHub
    def add_issue_item(self, title, number, labels):
        self._n += 1
        iid = f"ITEM_{self._n}"
        self._items[iid] = {"status": brainstorm.S_IDEA,
                            "content": {"title": title, "number": number,
                                        "labels": {"nodes": [{"name": l}
                                                             for l in labels]}}}
        return iid

    def move_column(self, item_id, status):
        self._items[item_id]["status"] = status

    def set_issue_labels(self, item_id, labels):
        self._items[item_id]["content"]["labels"] = {
            "nodes": [{"name": l} for l in labels]}


def _normalize_item(node):
    """Aplati un node GraphQL en dict simple :
    {item_id, status, is_issue, issue_number, labels[], title}."""
    fv = node.get("fieldValueByName") or {}
    col_name = (fv.get("name") or "").lower()
    status = OPTION_STATUS.get(col_name)        # None si pas de colonne posee
    content = node.get("content") or {}
    is_issue = "number" in content
    labels = [l["name"] for l in
              ((content.get("labels") or {}).get("nodes") or [])]
    return {"item_id": node.get("id"), "status": status, "is_issue": is_issue,
            "issue_number": content.get("number"), "labels": labels,
            "title": content.get("title", "")}


# --------------------------------------------------------------------------- #
# board projet
# --------------------------------------------------------------------------- #
class ProjectBoard:
    """Synchronise des idees avec les items d'un Project v2."""

    def __init__(self, transport, owner, number):
        self.tp = transport
        self.owner = owner
        self.number = number
        self._pid = None
        self._field = None
        self._opts = None

    @property
    def project_id(self):
        if self._pid is None:
            self._pid = self.tp.project_id(self.owner, self.number)
        return self._pid

    def _ensure_field(self):
        if self._field is None:
            self._field, self._opts = self.tp.status_field(self.project_id)
        return self._field, self._opts

    # --- push : idee -> draft item ----------------------------------------
    def push(self, idea):
        """Cree un draft item depuis l'idee (corps Markdown = les 3 couches).
        Retourne l'item_id (stocke dans idea.issue par reutilisation du champ)."""
        item_id = self.tp.add_draft(self.project_id, idea.title,
                                    idea.as_markdown())
        idea.issue = item_id
        return item_id

    def set_status(self, item_id, status):
        field_id, opts = self._ensure_field()
        opt_name = STATUS_OPTION.get(status, "Idea").lower()
        if opt_name not in opts:
            raise KeyError(
                f"option '{opt_name}' absente du champ Status ; "
                f"options dispo : {list(opts)}")
        return self.tp.set_status(self.project_id, item_id, field_id,
                                  opts[opt_name])

    # --- lecture : items + statut effectif (colonne OU label) -------------
    def items(self):
        """Genere les items normalises du Project."""
        yield from self.tp.iter_items(self.project_id)

    def effective_status(self, item):
        """Statut declencheur : la COLONNE prime ; si l'item est une Issue et
        porte un label state:*, ce label peut aussi declencher. On prend le
        plus 'avance' des deux ? Non : on declenche si l'UN des deux dit prod.
        Retourne (status_colonne, status_label_ou_None)."""
        col = item.get("status")
        lbl = None
        if item.get("is_issue"):
            for l in item.get("labels", []):
                if l in LABEL_TO_STATE:
                    lbl = LABEL_TO_STATE[l]
                    break
        return col, lbl


# --------------------------------------------------------------------------- #
# watcher double-declencheur (colonne TOUJOURS, label si Issue)
# --------------------------------------------------------------------------- #
def watch_once(board, ideas_by_item, actions, confirm=None, store=None):
    """Pour chaque item du Project : si la COLONNE = Prod, OU (item Issue ET
    label state:prod), et que l'idee correspondante a de l'acte en attente,
    demande confirmation et execute. Genere (item, idea, evenement)."""
    from github_board import default_confirm
    confirm = confirm or default_confirm
    for item in board.items():
        col, lbl = board.effective_status(item)
        triggered = (col == brainstorm.S_PROD) or (lbl == brainstorm.S_PROD)
        if not triggered:
            continue
        idea = ideas_by_item.get(item["item_id"])
        if idea is None or not idea.pending():
            continue
        src = "colonne" if col == brainstorm.S_PROD else "label"
        idea.touch(f"declencheur prod via {src}")
        if confirm(idea):
            ran = []
            for key in list(idea.acted):
                act = actions.get(key)
                if act:
                    act(idea)
                    ran.append(key)
            idea.mark_applied()
            idea.touch(f"execute: {', '.join(ran) or '(aucune action)'}")
            if store:
                idea.save(store)
            yield item, idea, f"execute via {src} ({', '.join(ran) or 'rien'})"
        else:
            idea.touch("execution refusee")
            yield item, idea, "refuse"


if __name__ == "__main__":
    print("transports :", ProjectV2Transport.name, "/", StubProjectV2.name)
    print("statuts <-> options Project :")
    for st, opt in STATUS_OPTION.items():
        print(f"  {st:6} <-> colonne '{opt}'")
    print("declencheur prod : colonne 'Prod' TOUJOURS ; label state:prod si Issue")

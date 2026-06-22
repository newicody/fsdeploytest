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
# requetes GraphQL (constantes) -- a valider en reel
# --------------------------------------------------------------------------- #
Q_PROJECT_ID_USER = """
query($owner:String!, $number:Int!){
  user(login:$owner){ projectV2(number:$number){ id title } }
}"""

Q_PROJECT_ID_ORG = """
query($owner:String!, $number:Int!){
  organization(login:$owner){ projectV2(number:$number){ id title } }
}"""

Q_STATUS_FIELD = """
query($projectId:ID!){
  node(id:$projectId){ ... on ProjectV2 {
    fields(first:30){ nodes{
      ... on ProjectV2SingleSelectField { id name options { id name } }
    } }
  } }
}"""

Q_ITEMS = """
query($projectId:ID!, $cursor:String){
  node(id:$projectId){ ... on ProjectV2 {
    items(first:50, after:$cursor){
      pageInfo{ hasNextPage endCursor }
      nodes{
        id
        fieldValueByName(name:"%s"){
          ... on ProjectV2ItemFieldSingleSelectValue { name }
        }
        content{
          ... on Issue   { number title labels(first:30){ nodes{ name } } }
          ... on DraftIssue { title }
        }
      }
    }
  } }
}""" % STATUS_FIELD_NAME

M_ADD_DRAFT = """
mutation($projectId:ID!, $title:String!, $body:String!){
  addProjectV2DraftIssue(input:{projectId:$projectId, title:$title, body:$body}){
    projectItem{ id }
  }
}"""

M_SET_STATUS = """
mutation($projectId:ID!, $itemId:ID!, $fieldId:ID!, $optionId:String!){
  updateProjectV2ItemFieldValue(input:{
    projectId:$projectId, itemId:$itemId, fieldId:$fieldId,
    value:{ singleSelectOptionId:$optionId }
  }){ projectV2Item{ id } }
}"""


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


class ProjectV2Transport(ProjectTransport):
    """Reel : GraphQL via api.github.com/graphql. A VALIDER en reel."""
    name = "graphql"
    API = "https://api.github.com/graphql"

    def __init__(self, token=None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("token absent (GITHUB_TOKEN, scope project)")

    def _gql(self, query, variables):
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(self.API, data=payload, method="POST",
                                     headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "brainstorm-project"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GraphQL {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub injoignable: {e.reason}") from e
        if data.get("errors"):
            raise RuntimeError("GraphQL errors: "
                               + json.dumps(data["errors"])[:200])
        return data.get("data", {})

    def project_id(self, owner, number):
        # 1. Tente de résoudre en tant que User
        try:
            d = self._gql(Q_PROJECT_ID_USER, {"owner": owner, "number": int(number)})
            node = (d.get("user") or {}).get("projectV2")
            if node and node.get("id"):
                return node["id"]
        except RuntimeError as e:
            # Ne propage que les vraies erreurs (ex: token invalide), ignore "GraphQL errors" (introuvable)
            if "GraphQL errors" not in str(e):
                raise

        # 2. Tente de résoudre en tant qu'Organisation
        try:
            d = self._gql(Q_PROJECT_ID_ORG, {"owner": owner, "number": int(number)})
            node = (d.get("organization") or {}).get("projectV2")
            if node and node.get("id"):
                return node["id"]
        except RuntimeError as e:
            if "GraphQL errors" not in str(e):
                raise
        
        # 3. Échec final
        raise RuntimeError(f"Project #{number} introuvable pour {owner} (Ni User, Ni Organisation)")

    def status_field(self, project_id):
        d = self._gql(Q_STATUS_FIELD, {"projectId": project_id})
        nodes = (((d.get("node") or {}).get("fields") or {}).get("nodes") or [])
        for f in nodes:
            if f and f.get("name") == STATUS_FIELD_NAME and "options" in f:
                opts = {o["name"].lower(): o["id"] for o in f["options"]}
                return f["id"], opts
        raise RuntimeError(f"champ '{STATUS_FIELD_NAME}' single-select absent "
                           f"du Project (cree-le avec les options "
                           f"{list(STATUS_OPTION.values())})")

    def iter_items(self, project_id):
        cursor = None
        while True:
            d = self._gql(Q_ITEMS, {"projectId": project_id, "cursor": cursor})
            items = ((d.get("node") or {}).get("items") or {})
            for node in items.get("nodes", []):
                yield _normalize_item(node)
            page = items.get("pageInfo", {})
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

    def add_draft(self, project_id, title, body):
        d = self._gql(M_ADD_DRAFT,
                      {"projectId": project_id, "title": title, "body": body})
        return d["addProjectV2DraftIssue"]["projectItem"]["id"]

    def set_status(self, project_id, item_id, field_id, option_id):
        self._gql(M_SET_STATUS, {"projectId": project_id, "itemId": item_id,
                                 "fieldId": field_id, "optionId": option_id})
        return True


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

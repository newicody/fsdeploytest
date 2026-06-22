#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
github_api.py -- transports GitHub STDLIB-ONLY (urllib), extraits pour etre
EMBARQUABLES dans l'initramfs : init.py les utilise pour le gate de boot sans
tirer brainstorm/ov_pipelines (qui, eux, ne sont PAS dans l'initramfs).

Contient :
  - ProjectV2Transport : client GraphQL Projects v2 BRUT (project_id, champ
    single-select par nom, items, draft, set field). Aucune semantique metier
    (pas de brainstorm) : il rend les colonnes telles quelles.
  - BoardAsk : protocole GENERIQUE "poser une question structuree au board et
    attendre une reponse". Le ticket encode kernel/situation/tache/message et la
    table reponse->colonne ; la REPONSE = la colonne single-select dans laquelle
    l'item est deplace (operateur ou automation). Reutilise par le gate de boot
    (init.py) et utilisable ailleurs.

github_project.py reutilise ProjectV2Transport (il le sous-classe pour ajouter
le vocabulaire brainstorm) : pas de duplication du reseau. Stdlib uniquement.
"""
import json
import os
import urllib.error
import urllib.request

GRAPHQL_API = "https://api.github.com/graphql"

Q_PROJECT_ID_USER = """
query($owner:String!, $number:Int!){
  user(login:$owner){ projectV2(number:$number){ id title } }
}"""

Q_PROJECT_ID_ORG = """
query($owner:String!, $number:Int!){
  organization(login:$owner){ projectV2(number:$number){ id title } }
}"""

Q_FIELD = """
query($projectId:ID!){
  node(id:$projectId){ ... on ProjectV2 {
    fields(first:50){ nodes{
      ... on ProjectV2SingleSelectField { id name options { id name } }
    } }
  } }
}"""

# nom du champ passe en VARIABLE GraphQL (pas de %-format) -> generique.
Q_ITEMS = """
query($projectId:ID!, $field:String!, $cursor:String){
  node(id:$projectId){ ... on ProjectV2 {
    items(first:50, after:$cursor){
      pageInfo{ hasNextPage endCursor }
      nodes{
        id
        fieldValueByName(name:$field){
          ... on ProjectV2ItemFieldSingleSelectValue { name }
        }
        content{
          ... on Issue   { number title labels(first:30){ nodes{ name } } }
          ... on DraftIssue { title }
        }
      }
    }
  } }
}"""

M_ADD_DRAFT = """
mutation($projectId:ID!, $title:String!, $body:String!){
  addProjectV2DraftIssue(input:{projectId:$projectId, title:$title, body:$body}){
    projectItem{ id }
  }
}"""

M_SET_FIELD = """
mutation($projectId:ID!, $itemId:ID!, $fieldId:ID!, $optionId:String!){
  updateProjectV2ItemFieldValue(input:{
    projectId:$projectId, itemId:$itemId, fieldId:$fieldId,
    value:{ singleSelectOptionId:$optionId }
  }){ projectV2Item{ id } }
}"""


def normalize_item(node):
    """Aplati un node GraphQL en dict BRUT (aucune semantique metier) :
    {item_id, column, is_issue, issue_number, labels[], title}. 'column' = nom
    (minuscule) de l'option single-select posee, ou '' si aucune."""
    fv = node.get("fieldValueByName") or {}
    content = node.get("content") or {}
    return {
        "item_id": node.get("id"),
        "column": (fv.get("name") or "").lower(),
        "is_issue": "number" in content,
        "issue_number": content.get("number"),
        "labels": [l["name"] for l in
                   ((content.get("labels") or {}).get("nodes") or [])],
        "title": content.get("title", ""),
    }


class ProjectV2Transport:
    """Client GraphQL Projects v2 brut. Stdlib (urllib). A valider en reel via
    test_project.py."""
    name = "graphql"
    API = GRAPHQL_API

    def __init__(self, token=None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("token absent (GITHUB_TOKEN, scope project)")

    def _gql(self, query, variables, tolerate_notfound=False, timeout=30):
        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(self.API, data=payload, method="POST",
                                     headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "fsdeploy-api"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GraphQL {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub injoignable: {e.reason}") from e
        if data.get("errors"):
            errs = data["errors"]
            # NOT_FOUND isole = on sonde user() pour un owner organisation (ou
            # l'inverse) : la donnee utile de l'AUTRE type est presente. On ne
            # leve pas dans CE cas precis ; toute autre erreur (token, scope,
            # reseau) continue de lever.
            if tolerate_notfound and all((e or {}).get("type") == "NOT_FOUND"
                                         for e in errs):
                return data.get("data", {})
            raise RuntimeError("GraphQL errors: " + json.dumps(errs)[:200])
        return data.get("data", {})

    def project_id(self, owner, number):
        # Projects v2 vit SOIT sous user(), SOIT sous organization() : pas
        # d'entree polymorphe unique, et on ne peut pas sonder les deux dans une
        # requete (le mauvais type renvoie NOT_FOUND -> ferait planter _gql). On
        # essaie USER d'abord (compte perso), ORG en repli ; NOT_FOUND tolere.
        variables = {"owner": owner, "number": int(number)}
        for kind, query in (("user", Q_PROJECT_ID_USER),
                            ("organization", Q_PROJECT_ID_ORG)):
            d = self._gql(query, variables, tolerate_notfound=True)
            node = (d.get(kind) or {}).get("projectV2")
            if node and node.get("id"):
                return node["id"]
        raise RuntimeError(
            f"Project #{number} introuvable pour '{owner}' (ni user ni "
            f"organization). Verifie owner/number et le scope du token "
            f"(classic: 'project' ; fine-grained: Projects read/write).")

    def single_select_field(self, project_id, name):
        """Retourne (field_id, {option_name_minuscule: option_id}) pour le champ
        single-select 'name'. Leve si absent."""
        d = self._gql(Q_FIELD, {"projectId": project_id})
        nodes = (((d.get("node") or {}).get("fields") or {}).get("nodes") or [])
        for f in nodes:
            if f and f.get("name") == name and "options" in f:
                return f["id"], {o["name"].lower(): o["id"] for o in f["options"]}
        raise RuntimeError(f"champ single-select '{name}' absent du Project "
                           "(cree-le, ou ajuste le nom du champ).")

    def raw_items(self, project_id, field_name):
        """Genere les NODES GraphQL bruts (pour un appelant qui veut sa propre
        normalisation, ex github_project)."""
        cursor = None
        while True:
            d = self._gql(Q_ITEMS, {"projectId": project_id,
                                    "field": field_name, "cursor": cursor})
            items = ((d.get("node") or {}).get("items") or {})
            for node in items.get("nodes", []):
                yield node
            page = items.get("pageInfo", {})
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

    def iter_items(self, project_id, field_name):
        """Genere les items normalises bruts (dict 'column')."""
        for node in self.raw_items(project_id, field_name):
            yield normalize_item(node)

    def item_column(self, project_id, item_id, field_name):
        """Colonne (minuscule) posee sur UN item precis, ou '' . Pour le poll."""
        for it in self.iter_items(project_id, field_name):
            if it["item_id"] == item_id:
                return it["column"]
        return ""

    def add_draft(self, project_id, title, body):
        d = self._gql(M_ADD_DRAFT,
                      {"projectId": project_id, "title": title, "body": body})
        return d["addProjectV2DraftIssue"]["projectItem"]["id"]

    def set_field(self, project_id, item_id, field_id, option_id):
        self._gql(M_SET_FIELD, {"projectId": project_id, "itemId": item_id,
                                "fieldId": field_id, "optionId": option_id})
        return True


class BoardAsk:
    """Protocole generique : poser une question structuree au board et attendre
    une reponse. La REPONSE = la colonne single-select dans laquelle l'item est
    deplace (operateur ou automation). Ticket : titre machine-parseable
    'ask:<kernel>:<situation>:<tache>' + corps Markdown (message + table
    reponse->colonne). Aucune dependance metier -> embarquable dans l'initramfs.

    responses : dict ORDONNE {reponse_label: nom_colonne}. Les colonnes doivent
    exister comme options du champ single-select (field_name, defaut 'Status')."""

    def __init__(self, transport, owner, number, field_name="Status"):
        self.tp = transport
        self.owner = owner
        self.number = number
        self.field_name = field_name
        self._pid = None

    def project_id(self):
        if self._pid is None:
            self._pid = self.tp.project_id(self.owner, self.number)
        return self._pid

    def ask(self, kernel, situation, tache, message, responses):
        """Cree le ticket-question, renvoie item_id. Verifie que les colonnes
        cibles existent dans le champ."""
        pid = self.project_id()
        _fid, opts = self.tp.single_select_field(pid, self.field_name)
        missing = [c for c in responses.values() if c.lower() not in opts]
        if missing:
            raise RuntimeError(
                f"colonnes absentes du champ '{self.field_name}' : {missing} "
                f"(disponibles : {sorted(opts)})")
        title = f"ask:{kernel}:{situation}:{tache}"
        lines = ["## Decision requise", "", message, "",
                 "Repondre en deplacant cette carte dans une colonne :"]
        for label, col in responses.items():
            lines.append(f"- **{col}** -> {label}")
        return self.tp.add_draft(pid, title, "\n".join(lines))

    def poll(self, item_id, responses):
        """Lit la colonne de l'item ; renvoie le label de reponse si la colonne
        correspond a l'une des reponses, sinon None (= toujours en attente)."""
        pid = self.project_id()
        col = self.tp.item_column(pid, item_id, self.field_name)
        if not col:
            return None
        inv = {v.lower(): k for k, v in responses.items()}
        return inv.get(col)


class StubProjectV2Api:
    """Stub memoire (sans GraphQL ni brainstorm) pour tester BoardAsk."""
    name = "stub-api"

    def __init__(self, options=("Idea", "WIP", "Dev", "Prod", "Drop")):
        self._pid = "PRJ_stub"
        self._opts = {o.lower(): f"OPT_{o}" for o in options}
        self._items = {}
        self._n = 0

    def project_id(self, owner, number):
        return self._pid

    def single_select_field(self, project_id, name):
        return "FLD_" + name, dict(self._opts)

    def iter_items(self, project_id, field_name):
        for iid, it in self._items.items():
            yield {"item_id": iid, "column": it["column"], "is_issue": False,
                   "issue_number": None, "labels": [], "title": it["title"]}

    def item_column(self, project_id, item_id, field_name):
        return self._items.get(item_id, {}).get("column", "")

    def add_draft(self, project_id, title, body):
        self._n += 1
        iid = f"ITEM_{self._n}"
        self._items[iid] = {"title": title, "column": ""}
        return iid

    def set_field(self, project_id, item_id, field_id, option_id):
        return True

    # aide test : simuler le deplacement humain en colonne
    def move(self, item_id, column):
        self._items[item_id]["column"] = column.lower()

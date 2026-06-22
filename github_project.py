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

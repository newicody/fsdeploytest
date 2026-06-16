#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
github_board.py — pont board GitHub <-> fiches idee (orchestration, pas calcul).

GitHub ne fait QUE l'orchestration et l'affichage (Issues/Projects). Tout le
calcul reste local (OpenVINO via brainstorm/rag). Ce qui transite = du TEXTE
(corps d'Issue Markdown + labels), jamais de binaire.

Mapping :
  Idea  <-> Issue GitHub
  status <-> label  state:idea | state:wip | state:dev | state:prod | state:drop
  acted/candidats   -> rendus dans le corps de l'Issue (idea.as_markdown)

Transport interchangeable (meme pattern que ov_pipelines) :
  - GitHubTransport : reel, api.github.com (urllib, token)
  - StubTransport   : en memoire, pour dev/tests hors-ligne

Flux de declenchement (ce que tu as decrit) :
  tu changes le label en 'state:prod' sur GitHub
    -> le serveur local (watch) detecte le passage en prod ET de l'acte en
       attente (pending)
    -> il affiche sur la machine : "etes-vous d'accord avec ces changements :"
       + la liste de l'acte
    -> si tu valides -> il lance l'action (compilation...) puis mark_applied()
       et repousse le nouvel etat sur l'Issue.

Stdlib + brainstorm.
"""
import json
import os
import urllib.error
import urllib.request

import brainstorm

STATE_LABELS = {
    brainstorm.S_IDEA: "state:idea",
    brainstorm.S_WIP:  "state:wip",
    brainstorm.S_DEV:  "state:dev",
    brainstorm.S_PROD: "state:prod",
    brainstorm.S_DROP: "state:drop",
}
LABEL_TO_STATE = {v: k for k, v in STATE_LABELS.items()}


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class Transport:
    name = "base"

    def list_issues(self): raise NotImplementedError
    def get_issue(self, number): raise NotImplementedError
    def create_issue(self, title, body, labels): raise NotImplementedError
    def update_issue(self, number, title=None, body=None, labels=None,
                     state=None): raise NotImplementedError
    def add_comment(self, number, body): raise NotImplementedError


class StubTransport(Transport):
    """GitHub simule en memoire : permet de tester tout le flux board hors-ligne."""
    name = "stub"

    def __init__(self):
        self._issues = {}
        self._n = 0
        self.comments = {}

    def list_issues(self):
        return [dict(i) for i in self._issues.values()]

    def get_issue(self, number):
        if number not in self._issues:
            raise KeyError(f"issue #{number} inexistante (stub)")
        return dict(self._issues[number])

    def create_issue(self, title, body, labels):
        self._n += 1
        self._issues[self._n] = {"number": self._n, "title": title,
                                 "body": body, "labels": list(labels),
                                 "state": "open"}
        self.comments[self._n] = []
        return dict(self._issues[self._n])

    def update_issue(self, number, title=None, body=None, labels=None,
                     state=None):
        it = self._issues[number]
        if title is not None: it["title"] = title
        if body is not None: it["body"] = body
        if labels is not None: it["labels"] = list(labels)
        if state is not None: it["state"] = state
        return dict(it)

    def add_comment(self, number, body):
        self.comments.setdefault(number, []).append(body)
        return {"number": number, "body": body}

    # aide aux tests : simuler une action humaine cote GitHub
    def set_label_state(self, number, state):
        labels = [l for l in self._issues[number]["labels"]
                  if not l.startswith("state:")]
        labels.append(STATE_LABELS[state])
        self._issues[number]["labels"] = labels


class GitHubTransport(Transport):
    """Reel : api.github.com (REST v3). Token via argument ou env GITHUB_TOKEN.
    repo au format 'owner/name' (ex. 'newicody/brainstorm')."""
    name = "github"
    API = "https://api.github.com"

    def __init__(self, repo, token=None):
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("token GitHub absent (arg token ou GITHUB_TOKEN)")

    def _req(self, method, path, payload=None):
        url = self.API + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "brainstorm-board",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"GitHub {method} {path} -> {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub injoignable : {e.reason}") from e

    def list_issues(self):
        return self._req("GET", f"/repos/{self.repo}/issues?state=all&per_page=100")

    def get_issue(self, number):
        return self._req("GET", f"/repos/{self.repo}/issues/{number}")

    def create_issue(self, title, body, labels):
        return self._req("POST", f"/repos/{self.repo}/issues",
                         {"title": title, "body": body, "labels": list(labels)})

    def update_issue(self, number, title=None, body=None, labels=None,
                     state=None):
        payload = {}
        if title is not None: payload["title"] = title
        if body is not None: payload["body"] = body
        if labels is not None: payload["labels"] = list(labels)
        if state is not None: payload["state"] = state
        return self._req("PATCH", f"/repos/{self.repo}/issues/{number}", payload)

    def add_comment(self, number, body):
        return self._req("POST",
                         f"/repos/{self.repo}/issues/{number}/comments",
                         {"body": body})


# --------------------------------------------------------------------------- #
# board
# --------------------------------------------------------------------------- #
def _domain_labels(idea):
    return [f"domain:{d}" for d in idea.domains]


def _state_from_labels(labels):
    for l in labels:
        name = l["name"] if isinstance(l, dict) else l
        if name in LABEL_TO_STATE:
            return LABEL_TO_STATE[name]
    return None


class Board:
    """Synchronise des fiches Idea avec des Issues GitHub."""

    def __init__(self, transport):
        self.tp = transport

    # --- push : idee -> Issue ---------------------------------------------
    def push(self, idea):
        """Cree ou met a jour l'Issue correspondant a l'idee. Retourne le
        numero d'Issue (et le stocke dans idea.issue)."""
        labels = [STATE_LABELS[idea.status]] + _domain_labels(idea)
        body = idea.as_markdown()
        if idea.issue is None:
            res = self.tp.create_issue(idea.title, body, labels)
            idea.issue = res["number"]
        else:
            gh_state = "closed" if idea.status in (brainstorm.S_DROP,) else "open"
            self.tp.update_issue(idea.issue, title=idea.title, body=body,
                                 labels=labels, state=gh_state)
        return idea.issue

    # --- pull : Issue -> etat detecte -------------------------------------
    def pull_state(self, idea):
        """Lit l'etat (label) cote GitHub pour cette idee. Retourne le statut
        distant, ou None si l'Issue n'a pas de label d'etat."""
        if idea.issue is None:
            return None
        it = self.tp.get_issue(idea.issue)
        return _state_from_labels(it.get("labels", []))

    def sync_down(self, idea):
        """Applique l'etat GitHub a l'idee locale s'il differe. Retourne
        (change, ancien, nouveau)."""
        remote = self.pull_state(idea)
        if remote and remote != idea.status:
            old = idea.status
            idea.set_status(remote)
            return True, old, remote
        return False, idea.status, idea.status


# --------------------------------------------------------------------------- #
# watcher de confirmation (le declencheur que tu as decrit)
# --------------------------------------------------------------------------- #
def default_confirm(idea):
    """Confirmation interactive sur la machine locale. Remplacable par une UI."""
    print("\n" + "=" * 60)
    print(f"Idee '{idea.title}' passee en PROD sur le board.")
    print("Etes-vous d'accord avec ces changements actes ?\n")
    for line in idea.acted_list():
        print(f"   - {line}")
    if idea.chosen:
        print(f"\n   (solution retenue : candidat {idea.chosen})")
    print("=" * 60)
    ans = input("Lancer l'execution ? [y/N] ").strip().lower()
    return ans in ("y", "o")


def watch_once(board, ideas, actions, confirm=default_confirm, store=None):
    """Un tour de surveillance : pour chaque idee, lit l'etat GitHub ; si
    passage en PROD avec de l'acte EN ATTENTE, demande confirmation et execute.

    actions : {cle_acte: callable(idea) -> None}. Pour chaque cle presente dans
              idea.acted, l'action correspondante est lancee a la validation.
    Genere (idea, evenement) pour journalisation.
    """
    for idea in ideas:
        changed, old, new = board.sync_down(idea)
        if new == brainstorm.S_PROD and idea.pending():
            if confirm(idea):
                ran = []
                for key in list(idea.acted):
                    act = actions.get(key)
                    if act:
                        act(idea)
                        ran.append(key)
                idea.mark_applied()
                idea.touch(f"execute: {', '.join(ran) or '(aucune action mappee)'}")
                if store:
                    idea.save(store)
                board.push(idea)
                yield idea, f"execute ({', '.join(ran) or 'rien'})"
            else:
                idea.touch("execution refusee")
                yield idea, "refuse"
        elif changed:
            if store:
                idea.save(store)
            yield idea, f"etat {old} -> {new}"


if __name__ == "__main__":
    print("transports :", StubTransport.name, "/", GitHubTransport.name)
    print("etats <-> labels :")
    for st, lb in STATE_LABELS.items():
        print(f"  {st:6} <-> {lb}")

"""Iron Rod, fusion amont de septembre 2026 : le helper de lien dit la MEME chose
que la porte du dispatcher.

La fusion des 3695 commits amont a fait naitre ``_parent_still_gates``, un jumeau
ligne-a-ligne du predicat SQL de ``_parents_satisfied``, appele par ``create_task``
et ``link_tasks``. Sa docstring annoncait ce fichier de test. Il n'existait pas :
la garantie la plus importante du Kanban Iron Rod etait portee a DEUX endroits sans
un seul controle capable de passer au rouge si les deux divergeaient.

Contrat Iron Rod, inchange depuis le verdict PROD_NO_GO du 2026-08-28 : seule une
livraison reelle libere un descendant, soit ``done``, soit ``prod``, soit une carte
livree garee en ``todo`` avec son ``work_completed_at``. ``archived`` n'est PAS une
livraison et continue de bloquer. Un parent inconnu bloque (fail-closed).

Ce fichier tombe si le helper derive vers l'ancienne forme Iron Rod
(``work_completed_at IS NULL``), vers la forme amont (``NOT IN (done, archived)``),
ou si l'un des deux predicats bouge sans l'autre.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# Les dix statuts du board servi. Un statut ajoute en amont sans passer ici fera
# echouer test_tous_les_statuts_sont_couverts, qui est la pour ca.
TOUS_STATUTS = [
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
    "prod",
    "archived",
]

# Ce qui LIBERE un enfant, et rien d'autre.
LIBERENT = {"done", "prod"}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _parent_child(conn, parent_status, work_completed_at=None):
    parent = kb.create_task(conn, title="parent", assignee="setup")
    child = kb.create_task(conn, title="child", parents=[parent], assignee="setup")
    conn.execute(
        "UPDATE tasks SET status=?, work_completed_at=? WHERE id=?",
        (parent_status, work_completed_at, parent),
    )
    return parent, child


def test_tous_les_statuts_sont_couverts():
    """Un statut ajoute en amont doit forcer une decision ici, pas passer en silence."""
    assert set(TOUS_STATUTS) == set(kb.VALID_STATUSES), (
        "VALID_STATUSES a bouge : trancher explicitement si le nouveau statut "
        "libere un descendant, puis mettre TOUS_STATUTS a jour"
    )


@pytest.mark.parametrize("statut", TOUS_STATUTS)
def test_helper_et_dispatcher_repondent_pareil(conn, statut):
    """Le coeur du test : une seule question, une seule reponse, deux surfaces."""
    parent, child = _parent_child(conn, statut)
    bloque_selon_helper = kb._parent_still_gates(conn, parent)
    satisfait_selon_dispatcher = kb._parents_satisfied(conn, child)
    assert bloque_selon_helper is not satisfait_selon_dispatcher, (
        f"statut {statut!r} : le helper de lien dit bloque={bloque_selon_helper} "
        f"pendant que la porte du dispatcher dit satisfait={satisfait_selon_dispatcher}"
    )


@pytest.mark.parametrize("statut", TOUS_STATUTS)
def test_seuls_done_et_prod_liberent(conn, statut):
    parent, _child = _parent_child(conn, statut)
    attendu_bloque = statut not in LIBERENT
    assert kb._parent_still_gates(conn, parent) is attendu_bloque, (
        f"statut {statut!r} : attendu bloque={attendu_bloque}"
    )


def test_archive_bloque_toujours(conn):
    """Le piege historique : archiver un parent ne doit jamais liberer son aval."""
    parent, child = _parent_child(conn, "archived")
    assert kb._parent_still_gates(conn, parent) is True
    assert kb._parents_satisfied(conn, child) is False
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "todo"


def test_carte_livree_garee_en_todo_libere(conn):
    """Le seul cas ou un statut non terminal libere : livree, en attente de deploiement."""
    parent, child = _parent_child(conn, "todo", work_completed_at=1_750_000_000)
    assert kb._parent_still_gates(conn, parent) is False
    assert kb._parents_satisfied(conn, child) is True


def test_todo_sans_livraison_bloque(conn):
    """Le temoin du cas precedent : sans le tampon de livraison, ca bloque."""
    parent, child = _parent_child(conn, "todo")
    assert kb._parent_still_gates(conn, parent) is True
    assert kb._parents_satisfied(conn, child) is False


def test_creation_sous_parent_archive_ne_naît_pas_ready(conn):
    """Le chemin CREATE, celui que la fusion laisse en version amont pure.

    ``create_task`` ne decide pas lui-meme du statut initial : il delegue a
    ``initial_task_state`` (``hermes_cli/kanban_db_graph.py``), fichier que la
    fusion prend integralement cote amont, dont un commit s'intitule « gate
    create-with-parents like link; archived parent is terminal ». Si cette regle
    amont gouverne, une carte creee sous un parent ARCHIVE naitrait ``ready``
    pendant que ``_parents_satisfied`` refuse le claim : le board annoncerait une
    carte que le dispatcher ne prendra jamais.
    """
    parent = kb.create_task(conn, title="parent archive", assignee="setup")
    conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
    child = kb.create_task(conn, title="enfant", parents=[parent], assignee="setup")
    statut = kb.get_task(conn, child).status
    assert statut != "ready", (
        f"carte creee {statut!r} sous un parent archive, alors que la porte du "
        "dispatcher la refusera : board menteur"
    )
    assert kb._parents_satisfied(conn, child) is False


def test_parent_inconnu_bloque_fail_closed(conn):
    """Une ligne absente ne doit jamais valoir autorisation."""
    assert kb._parent_still_gates(conn, "t_inexistant_0000") is True

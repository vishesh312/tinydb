"""Tests for issue #5: pythonic existence checks with the ``in`` operator
(``doc_id in db`` / ``query in db``), as specified by the maintainer."""
from tinydb import TinyDB, Query, where
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'name': 'alice', 'email': 'alice@example.com'})
    db.insert({'name': 'bob', 'email': 'bob@example.com'})
    return db


def test_ac1_doc_id_membership_matches_contains():
    db = make_db()

    assert db.contains(doc_id=1)
    assert db.contains(doc_id=2)
    for doc_id in (3, 4, 99):
        assert (doc_id in db) == db.contains(doc_id=doc_id)
        assert doc_id not in db


def test_ac1_removed_doc_id_is_no_longer_a_member():
    db = make_db()

    db.remove(doc_ids=[1])

    assert 1 not in db
    assert not db.contains(doc_id=1)


def test_ac2_query_membership_matches_contains():
    db = make_db()
    User = Query()

    assert db.contains(User.email == 'bob@example.com')
    for q in (User.email == 'carol@example.com', where('name') == 'zed'):
        assert (q in db) == db.contains(q)
        assert q not in db


def test_ac3_unsupported_operand_is_rejected():
    db = make_db()

    try:
        found = {'name': 'alice'} in db
    except TypeError:
        found = False

    assert not found


def test_ac4_membership_on_named_table():
    db = make_db()
    table = db.table('people')
    table.insert({'name': 'carol'})

    assert table.contains(doc_id=1)
    assert 2 not in table
    assert (Query().name == 'bob') not in table
    assert len(table) == 1

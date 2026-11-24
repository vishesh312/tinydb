"""Tests for issue #4: optional ``limit`` argument for ``search()``."""
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple(
        {'task': i, 'status': 'open' if i % 2 else 'done'} for i in range(1, 11)
    )
    return db


Task = Query()


def test_ac1_limit_returns_first_n_matches_in_id_order():
    db = make_db()

    result = db.search(Task.status == 'open', limit=3)

    assert [d['task'] for d in result] == [1, 3, 5]
    assert [d.doc_id for d in result] == [1, 3, 5]


def test_ac2_limit_none_is_the_default_and_returns_everything():
    db = make_db()

    everything = db.search(Task.status == 'open')

    assert len(everything) == 5
    assert db.search(Task.status == 'open', limit=None) == everything


def test_ac3_fewer_matches_than_limit_returns_all_of_them():
    db = make_db()

    result = db.search(Task.status == 'done', limit=50)

    assert [d['task'] for d in result] == [2, 4, 6, 8, 10]


def test_ac4_limit_on_named_table():
    db = make_db()
    table = db.table('archive')
    table.insert_multiple({'task': t, 'status': 'open'} for t in 'abcd')

    result = table.search(Task.status == 'open', limit=2)

    assert [d['task'] for d in result] == ['a', 'b']


def test_ac5_limit_works_with_the_query_cache():
    db = make_db()
    q = Task.task > 2

    # populate the cache with the full result, then ask for a limited one
    assert len(db.search(q)) == 8
    assert [d['task'] for d in db.search(q, limit=2)] == [3, 4]
    # the cached full result is still intact
    assert len(db.search(q)) == 8
    assert db.count(q) == 8

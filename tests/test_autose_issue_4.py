import pytest

from tinydb import Query, TinyDB
from tinydb.storages import MemoryStorage


def test_ac1_limit_returns_first_matching_documents():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'status': 'open', 'n': 1},
        {'status': 'closed', 'n': 2},
        {'status': 'open', 'n': 3},
        {'status': 'open', 'n': 4},
    ])
    q = Query().status == 'open'

    assert db.search(q, limit=2) == [
        {'status': 'open', 'n': 1},
        {'status': 'open', 'n': 3},
    ]


def test_ac2_named_table_limit_exceeds_matches():
    db = TinyDB(storage=MemoryStorage)
    table = db.table('x')
    table.insert_multiple([{'n': 1}, {'n': 2}])
    q = Query().n > 0

    assert table.search(q, limit=5) == [{'n': 1}, {'n': 2}]


def test_ac3_explicit_none_returns_all_matches():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([{'n': 1}, {'n': 2}])
    q = Query().n > 0

    assert db.search(q, limit=None) == [{'n': 1}, {'n': 2}]


def test_ac4_zero_limit_returns_empty_list():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'n': 1})
    q = Query().n > 0

    assert db.search(q, limit=0) == []


def test_ac5_negative_limit_raises_value_error():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'n': 1})
    q = Query().n > 0

    with pytest.raises(ValueError):
        db.search(q, limit=-1)


def test_ac6_limited_search_does_not_truncate_cached_results():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([{'n': 1}, {'n': 2}])
    q = Query().n > 0
    db.search(q, limit=1)

    assert db.search(q) == [{'n': 1}, {'n': 2}]


def test_ac7_limit_applies_to_cached_unlimited_search():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([{'n': 1}, {'n': 2}])
    q = Query().n > 0
    db.search(q)

    assert db.search(q, limit=1) == [{'n': 1}]


def test_ac1_limit_can_be_passed_positionally():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([{'n': 1}, {'n': 2}])

    assert db.search(Query().n > 0, 1) == [{'n': 1}]

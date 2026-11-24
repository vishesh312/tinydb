import pytest

from tinydb import TinyDB
from tinydb.storages import JSONStorage, MemoryStorage


@pytest.fixture(params=['memory', 'json'])
def fresh_db(request, tmp_path):
    if request.param == 'json':
        database = TinyDB(tmp_path / 'test.json', storage=JSONStorage)
    else:
        database = TinyDB(storage=MemoryStorage)

    try:
        yield database
    finally:
        database.close()


@pytest.fixture(params=['default', 'foo'])
def selected_table(request, fresh_db):
    if request.param == 'default':
        return fresh_db
    return fresh_db.table('foo')


@pytest.fixture
def memory_db():
    database = TinyDB(storage=MemoryStorage)
    try:
        yield database
    finally:
        database.close()


def test_ac1_update_string_id(selected_table):
    assert selected_table.insert({'x': 1}) == 1

    affected = selected_table.update({'x': 2}, doc_ids=['1'])

    assert affected == [1]
    assert type(affected[0]) is int
    assert selected_table.get(doc_id=1) == {'x': 2}
    assert len(selected_table) == 1


def test_ac2_remove_string_id(selected_table):
    assert selected_table.insert({'x': 1}) == 1

    affected = selected_table.remove(doc_ids=['1'])

    assert affected == [1]
    assert type(affected[0]) is int
    assert selected_table.all() == []
    assert len(selected_table) == 0


def test_ac3_update_mixed_existing_and_missing_ids(memory_db):
    assert memory_db.insert_multiple([{'x': 1}, {'x': 2}, {'x': 3}]) == [
        1, 2, 3
    ]

    affected = memory_db.update({'x': 9}, doc_ids=['1', 2, '99', 100])

    assert len(affected) == 2
    assert set(affected) == {1, 2}
    assert all(type(doc_id) is int for doc_id in affected)
    assert memory_db.get(doc_id=1) == {'x': 9}
    assert memory_db.get(doc_id=2) == {'x': 9}
    assert memory_db.get(doc_id=3) == {'x': 3}
    assert len(memory_db) == 3


def test_ac4_remove_mixed_existing_and_missing_ids(memory_db):
    assert memory_db.insert_multiple([{'x': 1}, {'x': 2}, {'x': 3}]) == [
        1, 2, 3
    ]

    affected = memory_db.remove(doc_ids=['1', 2, '99', 100])

    assert len(affected) == 2
    assert set(affected) == {1, 2}
    assert all(type(doc_id) is int for doc_id in affected)
    assert memory_db.get(doc_id=1) is None
    assert memory_db.get(doc_id=2) is None
    assert memory_db.get(doc_id=3) == {'x': 3}
    assert len(memory_db) == 1


def test_ac5_update_only_missing_ids(memory_db):
    assert memory_db.insert({'x': 1}) == 1

    assert memory_db.update({'x': 9}, doc_ids=['99', 100]) == []

    assert memory_db.all() == [{'x': 1}]
    assert memory_db.get(doc_id=1) == {'x': 1}


def test_ac6_remove_only_missing_ids(memory_db):
    assert memory_db.insert({'x': 1}) == 1

    assert memory_db.remove(doc_ids=['99', 100]) == []

    assert memory_db.all() == [{'x': 1}]
    assert memory_db.get(doc_id=1) == {'x': 1}

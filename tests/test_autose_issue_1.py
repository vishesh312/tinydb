"""Tests for issue #1: update()/remove() must accept the same (string)
document IDs that get() accepts."""
import pytest

from tinydb import TinyDB
from tinydb.storages import MemoryStorage


@pytest.fixture
def mem_db():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple({'x': i} for i in (1, 2, 3))
    return db


def test_ac1_update_with_string_doc_id_updates_document(mem_db):
    assert mem_db.get(doc_id='1') == {'x': 1}

    assert mem_db.update({'x': 10}, doc_ids=['1']) == [1]

    assert mem_db.get(doc_id=1) == {'x': 10}
    assert mem_db.get(doc_id=2) == {'x': 2}
    assert mem_db.get(doc_id=3) == {'x': 3}


def test_ac1_update_with_string_doc_ids_and_callable(mem_db):
    def double(doc):
        doc['x'] *= 2

    assert mem_db.update(double, doc_ids=['2', '3']) == [2, 3]
    assert [d['x'] for d in mem_db.all()] == [1, 4, 6]


def test_ac2_remove_with_string_doc_ids_removes_documents(mem_db):
    assert mem_db.remove(doc_ids=['1', '3']) == [1, 3]

    assert len(mem_db) == 1
    assert mem_db.get(doc_id=1) is None
    assert mem_db.get(doc_id=3) is None
    assert mem_db.get(doc_id=2) == {'x': 2}


def test_ac3_mixed_int_and_string_ids(mem_db):
    assert mem_db.update({'x': 0}, doc_ids=[1, '2']) == [1, 2]
    assert [d['x'] for d in mem_db.all()] == [0, 0, 3]
    assert mem_db.remove(doc_ids=['3', 1]) == [3, 1]
    assert [d.doc_id for d in mem_db.all()] == [2]


def test_ac3_same_document_given_as_int_and_string(mem_db):
    assert mem_db.update({'x': 7}, doc_ids=[2, '2']) == [2]
    assert mem_db.remove(doc_ids=['2', 2]) == [2]
    assert [d.doc_id for d in mem_db.all()] == [1, 3]


def test_ac4_missing_ids_are_still_skipped(mem_db):
    assert mem_db.update({'x': 99}, doc_ids=['42']) == []
    assert mem_db.remove(doc_ids=['42', 42, 'abc']) == []
    assert mem_db.update({'x': 99}, doc_ids=['2', '42', 'abc']) == [2]
    assert len(mem_db) == 3
    # consistent with get(), which also finds nothing for these
    assert mem_db.get(doc_id='abc') is None
    assert mem_db.get(doc_ids=['42', 'abc']) == []


def test_ac5_named_table_with_json_storage(tmp_path):
    path = tmp_path / 'db.json'
    with TinyDB(path) as db:
        table = db.table('things')
        table.insert_multiple([{'v': 'a'}, {'v': 'b'}])
        assert table.update({'v': 'B'}, doc_ids=['2']) == [2]
        assert table.remove(doc_ids=['1']) == [1]

    with TinyDB(path) as db:
        docs = db.table('things').all()
        assert [(d.doc_id, d['v']) for d in docs] == [(2, 'B')]

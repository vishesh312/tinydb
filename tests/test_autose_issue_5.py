import pytest

from tinydb import Query, TinyDB
from tinydb.storages import MemoryStorage


def test_ac1_default_table_contains_document_id():
    db = TinyDB(storage=MemoryStorage)
    assert db.insert({'name': 'alice'}) == 1

    assert (1 in db) is True


def test_ac2_default_table_excludes_removed_document_id():
    db = TinyDB(storage=MemoryStorage)
    assert db.insert({'name': 'alice'}) == 1
    db.remove(doc_ids=[1])

    assert (1 in db) is False


def test_ac3_named_table_contains_document_id():
    db = TinyDB(storage=MemoryStorage)
    people = db.table('people')
    assert people.insert({'name': 'alice'}) == 1

    assert (1 in people) is True


def test_ac4_default_table_contains_matching_query():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'email': 'alice@example.com'})

    assert (Query().email == 'alice@example.com') in db


def test_ac5_named_table_excludes_nonmatching_query():
    db = TinyDB(storage=MemoryStorage)
    people = db.table('people')
    people.insert({'email': 'alice@example.com'})

    assert ((Query().email == 'bob@example.com') in people) is False


def test_ac6_named_table_accepts_callable():
    db = TinyDB(storage=MemoryStorage)
    people = db.table('people')
    people.insert({'name': 'alice'})

    assert (lambda doc: doc['name'] == 'alice') in people


def test_ac7_default_table_rejects_dictionary():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'name': 'alice'})

    with pytest.raises(TypeError):
        {'name': 'alice'} in db


def test_ac8_named_table_rejects_dictionary():
    db = TinyDB(storage=MemoryStorage)
    people = db.table('people')
    people.insert({'name': 'alice'})

    with pytest.raises(TypeError):
        {'name': 'alice'} in people

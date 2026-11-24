"""Tests for issue #3: ``tinydb.operations.append(field, value)``."""
from tinydb import TinyDB, where
from tinydb.operations import append
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'name': 'ticket-41', 'tags': ['bug']})
    db.insert({'name': 'ticket-42', 'tags': ['bug']})
    return db


def test_ac1_append_adds_value_to_end_of_list():
    db = make_db()

    updated = db.update(append('tags', 'urgent'), where('name') == 'ticket-42')

    assert updated == [2]
    assert db.get(doc_id=2)['tags'] == ['bug', 'urgent']
    # non-matching document is untouched
    assert db.get(doc_id=1)['tags'] == ['bug']


def test_ac1_append_accumulates_over_multiple_updates():
    db = make_db()

    db.update(append('tags', 'urgent'), where('name') == 'ticket-42')
    db.update(append('tags', 'backend'), where('name') == 'ticket-42')
    db.update(append('tags', 7), where('name') == 'ticket-42')

    assert db.get(doc_id=2)['tags'] == ['bug', 'urgent', 'backend', 7]


def test_ac2_append_creates_list_when_field_missing():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'name': 'old-doc'})

    db.update(append('tags', 'urgent'), where('name') == 'old-doc')

    assert db.get(doc_id=1)['tags'] == ['urgent']


def test_ac3_append_with_doc_ids():
    db = make_db()

    assert db.update(append('tags', 'wontfix'), doc_ids=[1]) == [1]

    assert db.get(doc_id=1)['tags'] == ['bug', 'wontfix']
    assert db.get(doc_id=2)['tags'] == ['bug']


def test_ac4_append_is_persisted_to_json_file(tmp_path):
    path = tmp_path / 'db.json'
    with TinyDB(path) as db:
        db.insert({'name': 'log', 'events': []})
        db.update(append('events', 'started'), where('name') == 'log')
        db.update(append('events', 'stopped'), where('name') == 'log')

    with TinyDB(path) as db:
        assert db.get(where('name') == 'log')['events'] == [
            'started', 'stopped'
        ]

from tinydb import Query, TinyDB
from tinydb.operations import append
from tinydb.storages import JSONStorage, MemoryStorage


def test_ac1_appends_after_existing_tag():
    with TinyDB(storage=MemoryStorage) as db:
        db.insert({'name': 'ticket-42', 'tags': ['bug']})

        db.update(append('tags', 'urgent'), Query().name == 'ticket-42')

        assert db.all() == [
            {'name': 'ticket-42', 'tags': ['bug', 'urgent']}
        ]


def test_ac2_appends_list_as_one_nested_element():
    with TinyDB(storage=MemoryStorage) as db:
        db.insert({'name': 'ticket-42', 'tags': []})

        db.update(append('tags', ['a', 'b']), Query().name == 'ticket-42')

        assert db.all() == [
            {'name': 'ticket-42', 'tags': [['a', 'b']]}
        ]


def test_ac3_creates_missing_list_field():
    with TinyDB(storage=MemoryStorage) as db:
        db.insert({'name': 'ticket-42'})

        db.update(append('tags', 'urgent'), Query().name == 'ticket-42')

        assert db.all() == [
            {'name': 'ticket-42', 'tags': ['urgent']}
        ]


def test_ac4_updates_only_document_matching_keyword_condition():
    with TinyDB(storage=MemoryStorage) as db:
        db.insert({'name': 'ticket-42', 'tags': ['bug']})
        db.insert({'name': 'ticket-43', 'tags': ['other']})

        db.update(append('tags', 'urgent'), cond=Query().name == 'ticket-42')

        assert db.get(Query().name == 'ticket-42') == {
            'name': 'ticket-42', 'tags': ['bug', 'urgent']
        }
        assert db.get(Query().name == 'ticket-43') == {
            'name': 'ticket-43', 'tags': ['other']
        }


def test_ac5_updates_only_selected_document_id():
    with TinyDB(storage=MemoryStorage) as db:
        target_id = db.insert({'name': 'ticket-42', 'tags': ['bug']})
        db.insert({'name': 'ticket-43', 'tags': ['other']})

        db.update(append('tags', 'urgent'), doc_ids=[target_id])

        assert db.get(doc_id=target_id) == {
            'name': 'ticket-42', 'tags': ['bug', 'urgent']
        }
        assert db.get(Query().name == 'ticket-43') == {
            'name': 'ticket-43', 'tags': ['other']
        }


def test_ac6_appended_tag_persists_after_json_storage_reopens(tmp_path):
    path = str(tmp_path / 'append.json')
    with TinyDB(path, storage=JSONStorage) as db:
        db.insert({'name': 'ticket-42', 'tags': ['bug']})

        db.update(append('tags', 'urgent'), Query().name == 'ticket-42')

    with TinyDB(path, storage=JSONStorage) as reopened_db:
        assert reopened_db.all() == [
            {'name': 'ticket-42', 'tags': ['bug', 'urgent']}
        ]

"""Tests for issue #2: search() with fragment() on different fields must not
return another field's (cached) results."""
from tinydb import TinyDB, Query, where
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.insert({'home': {'city': 'Paris'}, 'work': {'city': 'Rome'}})
    return db


def test_ac1_search_fragment_on_second_field_is_correct():
    db = make_db()

    home = db.search(Query().home.fragment({'city': 'Paris'}))
    work = db.search(Query().work.fragment({'city': 'Paris'}))

    assert len(home) == 1
    assert work == []


def test_ac1_repeated_searches_stay_correct():
    db = make_db()
    db.insert({'home': {'city': 'Rome'}, 'work': {'city': 'Paris'}})
    frag = {'city': 'Paris'}

    for _ in range(3):
        assert [d.doc_id for d in db.search(where('home').fragment(frag))] \
            == [1]
        assert [d.doc_id for d in db.search(where('work').fragment(frag))] \
            == [2]


def test_ac2_count_and_get_with_fragment_on_other_field():
    db = make_db()
    frag = {'city': 'Paris'}

    assert db.count(Query().home.fragment(frag)) == 1
    assert db.count(Query().work.fragment(frag)) == 0
    assert db.get(Query().work.fragment(frag)) is None


def test_ac3_fragment_combined_with_other_queries():
    db = make_db()
    frag = {'city': 'Paris'}
    has_home = Query().home.exists()

    assert len(db.search(Query().home.fragment(frag) & has_home)) == 1
    assert db.search(Query().work.fragment(frag) & has_home) == []


def test_ac4_results_follow_document_updates():
    db = make_db()
    frag = {'city': 'Paris'}

    assert db.search(Query().work.fragment(frag)) == []
    db.update({'work': {'city': 'Paris'}}, doc_ids=[1])
    assert len(db.search(Query().work.fragment(frag))) == 1

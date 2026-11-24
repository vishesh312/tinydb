from tinydb import Query, TinyDB
from tinydb.storages import MemoryStorage


def test_ac1_fragment_queries_for_different_paths_compare_unequal():
    q_home = Query().home.fragment({'city': 'Paris'})
    q_work = Query().work.fragment({'city': 'Paris'})

    assert (q_home == q_work) is False


def test_ac1_independent_same_path_fragment_queries_compare_equal():
    first = Query().home.fragment({'city': 'Paris'})
    second = Query().home.fragment({'city': 'Paris'})

    assert (first == second) is True


def test_ac1_nested_fragment_paths_compare_unequal():
    q_home = Query().addresses.home.fragment({'city': 'Paris'})
    q_work = Query().addresses.work.fragment({'city': 'Paris'})

    assert (q_home == q_work) is False


def test_ac2_work_search_after_matching_home_search():
    document = {'home': {'city': 'Paris'}, 'work': {'city': 'Rome'}}
    with TinyDB(storage=MemoryStorage) as db:
        db.insert(document)
        assert db.search(Query().home.fragment({'city': 'Paris'})) == [
            document
        ]

        assert db.search(Query().work.fragment({'city': 'Paris'})) == []


def test_ac3_home_search_after_empty_work_search():
    document = {'home': {'city': 'Paris'}, 'work': {'city': 'Rome'}}
    with TinyDB(storage=MemoryStorage) as db:
        db.insert(document)
        assert db.search(Query().work.fragment({'city': 'Paris'})) == []

        assert db.search(Query().home.fragment({'city': 'Paris'})) == [
            document
        ]


def test_ac4_work_count_after_matching_home_search():
    document = {'home': {'city': 'Paris'}, 'work': {'city': 'Rome'}}
    with TinyDB(storage=MemoryStorage) as db:
        db.insert(document)
        assert db.search(Query().home.fragment({'city': 'Paris'})) == [
            document
        ]

        assert db.count(Query().work.fragment({'city': 'Paris'})) == 0


def test_ac5_work_get_after_matching_home_search():
    document = {'home': {'city': 'Paris'}, 'work': {'city': 'Rome'}}
    with TinyDB(storage=MemoryStorage) as db:
        db.insert(document)
        assert db.search(Query().home.fragment({'city': 'Paris'})) == [
            document
        ]

        assert db.get(Query().work.fragment({'city': 'Paris'})) is None

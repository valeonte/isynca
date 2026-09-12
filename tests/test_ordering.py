from pathlib import PurePosixPath

from isynca.ordering import natural_key, natural_path_key


def test_numbered_names_sort_by_value():
    names = [f"Orbis 2013 - low res ({n} of 231).jpg" for n in (1, 10, 100, 2, 20, 3)]
    assert sorted(names, key=natural_key) == [
        f"Orbis 2013 - low res ({n} of 231).jpg" for n in (1, 2, 3, 10, 20, 100)
    ]


def test_text_still_sorts_as_text():
    assert sorted(["b", "a", "c"], key=natural_key) == ["a", "b", "c"]


def test_numbers_sort_before_text_at_the_same_position():
    assert sorted(["a", "1"], key=natural_key) == ["1", "a"]


def test_leading_zeros_keep_distinct_names_stable():
    assert sorted(["1", "01", "001"], key=natural_key) == ["001", "01", "1"]
    assert sorted(["001", "01", "1"], key=natural_key) == ["001", "01", "1"]


def test_a_name_that_is_a_prefix_sorts_first():
    assert sorted(["a1", "a"], key=natural_key) == ["a", "a1"]


def test_paths_compare_component_by_component():
    paths = [PurePosixPath(p) for p in ("a/10.jpg", "a/2.jpg", "a-x", "a/1.jpg")]
    assert [str(p) for p in sorted(paths, key=natural_path_key)] == [
        "a/1.jpg",
        "a/2.jpg",
        "a/10.jpg",
        "a-x",
    ]

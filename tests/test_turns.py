from core.turns import is_internal_message


def test_is_internal_message():
    assert is_internal_message({"role": "_checkpoint", "content": {"id": "x"}})
    assert not is_internal_message({"role": "user", "content": "hi"})
    assert not is_internal_message({"role": None})
    assert not is_internal_message("junk")

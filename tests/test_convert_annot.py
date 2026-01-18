import string

from hypothesis import given
from hypothesis import strategies as st

from codi.api.utils.convert_annot import AnnotOutput, reformat_mention

_NAME_STRATEGY = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(string.ascii_uppercase),
    st.text(alphabet=string.ascii_lowercase, min_size=0, max_size=8),
)


@given(_NAME_STRATEGY, st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=12))
def test_reformat_mention_uses_existing_member(name: str, tail: str) -> None:
    out: AnnotOutput = {"id": "set", "name": "set", "members": [{"id": "abc", "name": name}], "channels": []}
    message = f"hi {name}: {tail}"

    result = reformat_mention(message, out, names_list=[name])

    assert f"<@Uabc|{name}>" in result
    assert len(out["members"]) == 1


@given(_NAME_STRATEGY, st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=12))
def test_reformat_mention_adds_missing_member(name: str, tail: str) -> None:
    out: AnnotOutput = {"id": "set", "name": "set", "members": [], "channels": []}
    message = f"hi {name}: {tail}"

    result = reformat_mention(message, out, names_list=[name])

    assert len(out["members"]) == 1
    created = out["members"][0]
    assert created["name"] == name
    assert f"<@U{created['id']}|{name}>" in result


@given(_NAME_STRATEGY, st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=12))
def test_reformat_mention_ignores_unknown_name(name: str, tail: str) -> None:
    out: AnnotOutput = {"id": "set", "name": "set", "members": [], "channels": []}
    message = f"hi {name}: {tail}"

    result = reformat_mention(message, out, names_list=[])

    assert result == message
    assert out["members"] == []


@given(st.text(alphabet=string.ascii_lowercase + " ", min_size=1, max_size=20))
def test_reformat_mention_no_match_returns_original(message: str) -> None:
    out: AnnotOutput = {"id": "set", "name": "set", "members": [], "channels": []}

    result = reformat_mention(message, out, names_list=[])

    assert result == message
    assert out["members"] == []

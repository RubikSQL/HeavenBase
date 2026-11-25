import pytest

from heavenbase.capsule import Capsule, parse_docstring

pytestmark = pytest.mark.fast


def _list_templates(source: str | None = None) -> dict[str, object]:
    """List templates.

    Args:
        source (str | None): Optional template source such as `builtin` or
            `project`.

    Returns:
        dict[str, object]: Template payload.
    """

    return {"source": source}


def test_parse_docstring_preserves_wrapped_typed_google_argument_description() -> None:
    docstring = """List templates.

    Args:
        source (str | None): Optional template source such as `builtin` or
            `project`.

    Returns:
        dict[str, object]: Template payload.
    """

    parsed = parse_docstring(docstring)

    assert parsed["summary"] == "List templates."
    assert parsed["params"] == {"source": "Optional template source such as `builtin` or `project`."}
    assert parsed["returns"] == "dict[str, object]: Template payload."


def test_parse_docstring_preserves_wrapped_untyped_google_argument_description() -> None:
    docstring = """List templates.

    Args:
        source: Optional template source such as `builtin` or
            `project`.
        family: Optional family filter.
    """

    parsed = parse_docstring(docstring)

    assert parsed["params"] == {
        "source": "Optional template source such as `builtin` or `project`.",
        "family": "Optional family filter.",
    }


def test_capsule_capture_uses_wrapped_typed_google_argument_description() -> None:
    capsule = Capsule(_list_templates, namespace="tests", version="1")

    assert capsule.manifest.signature["parameters"][0]["description"] == ("Optional template source such as `builtin` or `project`.")

"""Static public-facade contract for a small external consumer."""

from pathlib import Path

from mypy import api as mypy_api


def test_external_consumer_typechecks(tmp_path):
    fixture = Path(__file__).parents[1] / "typing" / "external_consumer.py"

    stdout, stderr, status = mypy_api.run(
        [
            str(fixture),
            "--strict",
            "--follow-imports=silent",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
        ]
    )

    assert status == 0, stdout or stderr

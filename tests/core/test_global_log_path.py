import pytest

from heavenbase.utils import get_logger, redirect_logs, restore_logs

pytestmark = pytest.mark.fast


def test_a_saved_log_target_expands_variables_only_once(tmp_path, monkeypatch):
    location = "$HB_R8LLM_SUBDIR"
    literal = tmp_path / location
    substituted = tmp_path / "substituted"
    literal.mkdir()
    substituted.mkdir()
    monkeypatch.setenv("HB_R8LLM_ROOT", str(literal))
    monkeypatch.setenv("HB_R8LLM_SUBDIR", "substituted")

    try:
        redirect_logs("$HB_R8LLM_ROOT/output.log")
        get_logger("heavenbase.tests.r8llm").warning("literal log target")
        assert (literal / "output.log").is_file()
        assert "literal log target" in (literal / "output.log").read_text(encoding="utf-8")
        assert not (substituted / "output.log").exists()
    finally:
        restore_logs()

"""Smoke test: verifies conftest scaffolding (import + fixtures + mock seams)."""
import json
from unittest.mock import patch


def test_module_imports_and_functions_exist(appmod):
    for fn in ("_merge_audio_parts", "_identify_speakers", "_analyze_interview"):
        assert callable(getattr(appmod, fn))


def test_anthropic_seam_patchable(appmod, make_anthropic_client):
    payload = {"summary": "ok", "vacancy_fit": {"score": 8}}
    client = make_anthropic_client(text=json.dumps(payload))
    with patch.object(appmod.anthropic, "Anthropic", return_value=client):
        result = appmod._analyze_interview("t", "v", "claude-opus-4-8")
    assert result["summary"] == "ok"
    assert result["vacancy_fit"]["score"] == 8

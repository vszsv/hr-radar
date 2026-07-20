"""Unit tests for web_panel.app._identify_speakers.

The function relabels diarized speaker markers (**Спикер X:**) into role
labels (**🎤 Интервьюер:** / **👤 Кандидат:**) using an LLM-provided JSON
mapping. The anthropic client is injected, so we drive it with the
make_anthropic_client fixture from conftest.py.
"""
import pytest

INTERVIEWER = "**🎤 Интервьюер:**"
CANDIDATE = "**👤 Кандидат:**"


def test_no_markers_returns_unchanged_without_calling_client(appmod, make_anthropic_client):
    """Behavior 1: transcript with neither 'Спикер' nor 'Speaker' is returned
    verbatim and the client is never called."""
    transcript = "Просто текст без диаризации, привет и пока."
    client = make_anthropic_client(text='{"interviewer":"A","candidate":"B"}')

    result = appmod._identify_speakers(transcript, client)

    assert result == transcript
    client.messages.create.assert_not_called()


def test_happy_relabel(appmod, make_anthropic_client):
    """Behavior 2: A->Интервьюер, B->Кандидат, original markers gone."""
    transcript = "**Спикер A:** привет\n**Спикер B:** да"
    client = make_anthropic_client(text='{"interviewer":"A","candidate":"B"}')

    result = appmod._identify_speakers(transcript, client)

    assert INTERVIEWER in result
    assert CANDIDATE in result
    assert "**Спикер A:**" not in result
    assert "**Спикер B:**" not in result
    # surrounding text preserved
    assert "привет" in result and "да" in result
    client.messages.create.assert_called_once()


def test_swapped_roles(appmod, make_anthropic_client):
    """Behavior 3: interviewer=B, candidate=A -> B becomes Интервьюер,
    A becomes Кандидат."""
    transcript = "**Спикер A:** расскажите о себе\n**Спикер B:** меня зовут Иван"
    client = make_anthropic_client(text='{"interviewer":"B","candidate":"A"}')

    result = appmod._identify_speakers(transcript, client)

    assert f"{INTERVIEWER} меня зовут Иван" in result
    assert f"{CANDIDATE} расскажите о себе" in result
    assert "**Спикер A:**" not in result
    assert "**Спикер B:**" not in result


def test_json_embedded_in_prose(appmod, make_anthropic_client):
    """Behavior 4: regex extracts the {...} object out of surrounding prose."""
    transcript = "**Спикер A:** вопрос\n**Спикер B:** ответ"
    client = make_anthropic_client(text='Sure: {"interviewer":"A","candidate":"B"} done')

    result = appmod._identify_speakers(transcript, client)

    assert INTERVIEWER in result
    assert CANDIDATE in result
    assert "**Спикер A:**" not in result
    assert "**Спикер B:**" not in result


def test_malformed_response_no_json_returns_original(appmod, make_anthropic_client):
    """Behavior 5: no {...} in the response -> original transcript unchanged."""
    transcript = "**Спикер A:** привет\n**Спикер B:** да"
    client = make_anthropic_client(text="no json here")

    result = appmod._identify_speakers(transcript, client)

    assert result == transcript
    assert INTERVIEWER not in result
    assert CANDIDATE not in result


def test_missing_keys_use_defaults(appmod, make_anthropic_client):
    """Behavior 6: a JSON object lacking interviewer/candidate keys -> defaults
    interviewer='A', candidate='B' applied (A->Интервьюер, B->Кандидат).

    Note: the regex r'\\{[^}]+\\}' requires at least one char between braces, so
    a bare "{}" never matches and is handled by test_empty_braces_unchanged.
    This case uses a non-empty object to exercise the .get(..., default) path.
    """
    transcript = "**Спикер A:** привет\n**Спикер B:** да"
    client = make_anthropic_client(text='{"foo":"bar"}')

    result = appmod._identify_speakers(transcript, client)

    assert INTERVIEWER in result
    assert CANDIDATE in result
    assert "**Спикер A:**" not in result
    assert "**Спикер B:**" not in result


def test_empty_braces_unchanged(appmod, make_anthropic_client):
    """Behavior 6 (edge): bare '{}' does not match the regex r'\\{[^}]+\\}'
    (needs >=1 inner char), so the transcript is returned unchanged."""
    transcript = "**Спикер A:** привет\n**Спикер B:** да"
    client = make_anthropic_client(text="{}")

    result = appmod._identify_speakers(transcript, client)

    assert result == transcript
    assert INTERVIEWER not in result
    assert CANDIDATE not in result


def test_client_error_propagates(appmod, make_anthropic_client):
    """Behavior 7: the create() call is outside the try, so a client error
    propagates instead of being swallowed."""
    transcript = "**Спикер A:** привет\n**Спикер B:** да"
    client = make_anthropic_client(exc=RuntimeError("api down"))

    with pytest.raises(RuntimeError, match="api down"):
        appmod._identify_speakers(transcript, client)


def test_english_markers_call_client_but_no_relabel(appmod, make_anthropic_client):
    """Behavior 8: 'Speaker' passes the guard so the client IS called, but the
    Russian-only replacement leaves English labels untouched."""
    transcript = "**Speaker A:** hello\n**Speaker B:** hi"
    client = make_anthropic_client(text='{"interviewer":"A","candidate":"B"}')

    result = appmod._identify_speakers(transcript, client)

    assert result == transcript
    assert "**Speaker A:**" in result
    assert "**Speaker B:**" in result
    assert INTERVIEWER not in result
    assert CANDIDATE not in result
    client.messages.create.assert_called_once()

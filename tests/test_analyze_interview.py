"""Unit tests for web_panel.app._analyze_interview.

Covers JSON extraction (fences, prose, brace-depth scanner, trailing-comma
recovery), missing-section backfill, vacancy-prompt scoring-tail stripping,
PDF text inclusion, model forwarding, and the OpenAI fallback path
(usage-limits / rate_limit / overloaded substrings) vs. non-fallback re-raise.

The function builds its own anthropic client via anthropic.Anthropic(); we
patch web_panel.app.anthropic.Anthropic. The OpenAI fallback does
`from openai import OpenAI`, so we patch openai.OpenAI.
"""
import json

import pytest
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _full_payload(score=8, summary="Хорошее интервью."):
    """A complete, valid analysis payload with every required section."""
    return {
        "summary": summary,
        "vacancy_fit": {
            "score": score,
            "relevant_experience": ["управлял командой"],
            "hard_skills": ["BTL", "event"],
            "gaps": ["нет опыта в digital"],
        },
        "psychological_profile": {
            "thinking_type": "Системное мышление.",
            "communication_style": "Открытый.",
            "leadership": "Сильный лидер.",
            "stress_resistance": "Устойчив.",
            "motivation": "Рост.",
            "emotional_intelligence": "Высокая эмпатия.",
            "values_and_culture": "Совпадает.",
        },
        "speech_analysis": {
            "confidence_level": "Уверенный.",
            "specificity": "Конкретный.",
            "self_presentation": "Хорошая.",
            "red_flags": ["иногда уходит от ответа"],
        },
        "psychotype_analysis": {
            "accentuation": "Гипертимный.",
            "enneagram": "Тип 3.",
            "disc": "D-доминанта.",
            "conflict_style": "Сотрудничество.",
        },
        "overall": {
            "strengths": ["опыт", "энергия", "сеть контактов"],
            "risks": ["перегрузка", "детали", "digital"],
            "recommendation": "Брать.",
            "next_interview_questions": ["вопрос 1", "вопрос 2"],
        },
    }


def _patch_anthropic(appmod, make_anthropic_client, text=None, exc=None):
    """Context manager patching anthropic.Anthropic() to return a fake client.

    Returns the (patcher_cm, fake_client) so tests can introspect call_args.
    """
    client = make_anthropic_client(text=text, exc=exc)
    cm = patch.object(appmod.anthropic, "Anthropic", return_value=client)
    return cm, client


# --------------------------------------------------------------------------- #
# 1. happy anthropic                                                          #
# --------------------------------------------------------------------------- #

def test_happy_anthropic_full_json(appmod, make_anthropic_client):
    payload = _full_payload(score=9, summary="Кандидат силён.")
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(payload, ensure_ascii=False)
    )
    with cm:
        result = appmod._analyze_interview("🎤 Интервьюер: привет\n👤 Кандидат: да", "Ищем PM", "claude-x")

    assert result["summary"] == "Кандидат силён."
    assert result["vacancy_fit"]["score"] == 9
    assert result["vacancy_fit"]["hard_skills"] == ["BTL", "event"]


# --------------------------------------------------------------------------- #
# 2. markdown json fence                                                      #
# --------------------------------------------------------------------------- #

def test_markdown_json_fence_stripped(appmod, make_anthropic_client):
    payload = _full_payload(score=6)
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    cm, client = _patch_anthropic(appmod, make_anthropic_client, text=fenced)
    with cm:
        result = appmod._analyze_interview("t", "v", "m")

    assert result["vacancy_fit"]["score"] == 6
    assert result["summary"] == payload["summary"]


# --------------------------------------------------------------------------- #
# 3. plain fence                                                              #
# --------------------------------------------------------------------------- #

def test_plain_fence_stripped(appmod, make_anthropic_client):
    payload = _full_payload(score=5)
    fenced = "```\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    cm, client = _patch_anthropic(appmod, make_anthropic_client, text=fenced)
    with cm:
        result = appmod._analyze_interview("t", "v", "m")

    assert result["vacancy_fit"]["score"] == 5
    assert "psychological_profile" in result


# --------------------------------------------------------------------------- #
# 4. prose around JSON                                                        #
# --------------------------------------------------------------------------- #

def test_prose_around_json_extracted(appmod, make_anthropic_client):
    payload = _full_payload(score=4)
    text = "Here you go: " + json.dumps(payload, ensure_ascii=False) + " hope that helps"
    cm, client = _patch_anthropic(appmod, make_anthropic_client, text=text)
    with cm:
        result = appmod._analyze_interview("t", "v", "m")

    assert result["vacancy_fit"]["score"] == 4
    assert result["summary"] == payload["summary"]
    # Prose must not leak into the parsed dict
    assert "hope that helps" not in json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 5. trailing-comma recovery                                                  #
# --------------------------------------------------------------------------- #

def test_trailing_comma_recovery(appmod, make_anthropic_client):
    # Invalid JSON (trailing commas) — first json.loads fails, regex recovers.
    bad = '{"summary":"s","vacancy_fit":{"score":7,},}'
    cm, client = _patch_anthropic(appmod, make_anthropic_client, text=bad)
    with cm:
        result = appmod._analyze_interview("t", "v", "m")

    assert result["summary"] == "s"
    assert result["vacancy_fit"]["score"] == 7


# --------------------------------------------------------------------------- #
# 6. missing-section backfill                                                 #
# --------------------------------------------------------------------------- #

def test_missing_section_backfill(appmod, make_anthropic_client):
    partial = {
        "summary": "только две секции",
        "vacancy_fit": {"score": 3, "relevant_experience": [], "hard_skills": [], "gaps": []},
    }
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(partial, ensure_ascii=False)
    )
    with cm:
        result = appmod._analyze_interview("t", "v", "m")

    assert result["speech_analysis"] == {
        "confidence_level": "",
        "specificity": "",
        "self_presentation": "",
        "red_flags": [],
    }
    assert result["psychotype_analysis"] == {
        "accentuation": "",
        "enneagram": "",
        "disc": "",
        "conflict_style": "",
    }
    assert result["overall"] == {
        "strengths": [],
        "risks": [],
        "recommendation": "",
        "next_interview_questions": [],
    }
    # The sections that WERE present must survive untouched.
    assert result["summary"] == "только две секции"
    assert result["vacancy_fit"]["score"] == 3


# --------------------------------------------------------------------------- #
# 7. vacancy-prompt scoring strip                                             #
# --------------------------------------------------------------------------- #

def test_vacancy_prompt_scoring_tail_stripped(appmod, make_anthropic_client):
    payload = _full_payload()
    vacancy_prompt = "Ищем директора. Ответь СТРОГО JSON {score:..}"
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(payload, ensure_ascii=False)
    )
    with cm:
        appmod._analyze_interview("t", vacancy_prompt, "m")

    system = client.messages.create.call_args.kwargs["system"]
    assert "Ищем директора." in system
    assert "Ответь СТРОГО JSON" not in system
    assert "{score" not in system


# --------------------------------------------------------------------------- #
# 8. pdf text inclusion                                                       #
# --------------------------------------------------------------------------- #

def test_pdf_text_included_in_system_prompt(appmod, make_anthropic_client):
    payload = _full_payload()
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(payload, ensure_ascii=False)
    )
    with cm:
        appmod._analyze_interview(
            "t", "v", "m",
            vacancy_pdf_text="VPDFMARKER",
            resume_pdf_text="RPDFMARKER",
        )

    system = client.messages.create.call_args.kwargs["system"]
    assert "VPDFMARKER" in system
    assert "RPDFMARKER" in system


# --------------------------------------------------------------------------- #
# 9. model forwarded                                                          #
# --------------------------------------------------------------------------- #

def test_model_forwarded(appmod, make_anthropic_client):
    payload = _full_payload()
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(payload, ensure_ascii=False)
    )
    with cm:
        appmod._analyze_interview("t", "v", "claude-opus-4-8")

    assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-8"


# --------------------------------------------------------------------------- #
# 10. OpenAI fallback on usage limits                                         #
# --------------------------------------------------------------------------- #

def test_openai_fallback_on_usage_limits(appmod, make_anthropic_client, make_openai_client):
    payload = _full_payload(score=2, summary="из openai")
    cm, _ = _patch_anthropic(
        appmod, make_anthropic_client,
        exc=Exception("You have reached your workspace usage limits"),
    )
    oai_client = make_openai_client(json.dumps(payload, ensure_ascii=False))
    with cm, patch("openai.OpenAI", return_value=oai_client) as oai_factory:
        result = appmod._analyze_interview("t", "v", "claude-x")

    oai_factory.assert_called_once()
    assert oai_client.chat.completions.create.call_args.kwargs["model"] == "gpt-5.5"
    assert result["summary"] == "из openai"
    assert result["vacancy_fit"]["score"] == 2


# --------------------------------------------------------------------------- #
# 11. fallback on rate_limit and overloaded                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("err_msg", [
    "Error: rate_limit exceeded, try later",
    "The model is overloaded right now",
])
def test_openai_fallback_on_rate_limit_and_overloaded(
    appmod, make_anthropic_client, make_openai_client, err_msg
):
    payload = _full_payload(score=1, summary=f"fallback:{err_msg[:5]}")
    cm, _ = _patch_anthropic(appmod, make_anthropic_client, exc=Exception(err_msg))
    oai_client = make_openai_client(json.dumps(payload, ensure_ascii=False))
    with cm, patch("openai.OpenAI", return_value=oai_client) as oai_factory:
        result = appmod._analyze_interview("t", "v", "claude-x")

    oai_factory.assert_called_once()
    assert oai_client.chat.completions.create.call_args.kwargs["model"] == "gpt-5.5"
    assert result["summary"] == payload["summary"]


# --------------------------------------------------------------------------- #
# 12. non-fallback re-raise                                                   #
# --------------------------------------------------------------------------- #

def test_non_fallback_error_reraised(appmod, make_anthropic_client):
    cm, _ = _patch_anthropic(
        appmod, make_anthropic_client, exc=Exception("bad request 400")
    )
    with cm, patch("openai.OpenAI") as oai_factory:
        with pytest.raises(Exception) as excinfo:
            appmod._analyze_interview("t", "v", "claude-x")

    assert "bad request 400" in str(excinfo.value)
    oai_factory.assert_not_called()


# --------------------------------------------------------------------------- #
# 13. transcript actually reaches the model (anthropic path)                  #
# --------------------------------------------------------------------------- #

def test_transcript_forwarded_to_anthropic(appmod, make_anthropic_client):
    # Guards against a regression that drops/garbles the transcript: the
    # whole suite would otherwise stay green while the model gets nothing.
    payload = _full_payload()
    marker = "УНИКАЛЬНЫЙ_МАРКЕР_ТРАНСКРИПЦИИ_42"
    cm, client = _patch_anthropic(
        appmod, make_anthropic_client, text=json.dumps(payload, ensure_ascii=False)
    )
    with cm:
        appmod._analyze_interview(marker, "v", "m")

    user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert marker in user_content


# --------------------------------------------------------------------------- #
# 14. transcript + system prompt reach OpenAI on fallback                     #
# --------------------------------------------------------------------------- #

def test_transcript_and_system_forwarded_to_openai_on_fallback(
    appmod, make_anthropic_client, make_openai_client
):
    payload = _full_payload()
    marker = "УНИКАЛЬНЫЙ_МАРКЕР_ТРАНСКРИПЦИИ_99"
    cm, _ = _patch_anthropic(
        appmod, make_anthropic_client,
        exc=Exception("You have reached your workspace usage limits"),
    )
    oai_client = make_openai_client(json.dumps(payload, ensure_ascii=False))
    with cm, patch("openai.OpenAI", return_value=oai_client):
        appmod._analyze_interview(marker, "Ищем PM", "claude-x")

    messages = oai_client.chat.completions.create.call_args.kwargs["messages"]
    roles = {m["role"]: m["content"] for m in messages}
    # System prompt must be forwarded (not empty) and carry the vacancy text.
    assert "Ищем PM" in roles["system"]
    # Transcript must reach the user message.
    assert marker in roles["user"]

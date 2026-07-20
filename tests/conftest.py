"""Shared test scaffolding for web_panel.app unit tests.

The module web_panel/app.py is importable but has heavy import-time side
effects (loads .env, creates data dirs, inits candidate_journey.db via
CREATE TABLE IF NOT EXISTS). These are idempotent, so importing once here is
safe. All three functions under test (_merge_audio_parts, _identify_speakers,
_analyze_interview) are pure-ish given their external seams are mocked.

Mock seams:
  - _merge_audio_parts  -> patch web_panel.app.subprocess.run
  - _identify_speakers  -> client passed as argument (use make_anthropic_client)
  - _analyze_interview  -> patch web_panel.app.anthropic.Anthropic (returns a
                           client); OpenAI fallback -> patch openai.OpenAI
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import web_panel.app as _app  # noqa: E402


@pytest.fixture
def appmod():
    """The module under test."""
    return _app


@pytest.fixture
def make_anthropic_client():
    """Factory for a fake anthropic client.

    make_anthropic_client(text="...")    -> client whose messages.create()
                                            returns an object with
                                            .content[0].text == text
    make_anthropic_client(exc=Exception) -> client whose messages.create()
                                            raises exc
    """
    def _make(text=None, exc=None):
        client = MagicMock()
        if exc is not None:
            client.messages.create.side_effect = exc
        else:
            block = MagicMock()
            block.text = text
            client.messages.create.return_value = MagicMock(content=[block])
        return client
    return _make


@pytest.fixture
def make_openai_client():
    """Factory for a fake OpenAI client.

    make_openai_client("...") -> client whose chat.completions.create()
                                 returns an object with
                                 .choices[0].message.content == text
    """
    def _make(text):
        client = MagicMock()
        choice = MagicMock()
        choice.message.content = text
        client.chat.completions.create.return_value = MagicMock(choices=[choice])
        return client
    return _make

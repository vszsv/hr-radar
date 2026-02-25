"""Download and transcribe call recordings via OpenAI Whisper."""
import requests
from pathlib import Path
from openai import OpenAI
from config import OPENAI_API_KEY, AUDIO_DIR

client = OpenAI(api_key=OPENAI_API_KEY)


def download_audio(url: str, lead_id: int) -> Path | None:
    """Download audio file from Novofon CDN."""
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        ext = "mp3"
        if b"RIFF" in resp.content[:4]:
            ext = "wav"
        path = AUDIO_DIR / f"lead_{lead_id}.{ext}"
        path.write_bytes(resp.content)
        return path
    except Exception as e:
        print(f"[transcriber] Download failed for lead {lead_id}: {e}")
        return None


def transcribe(audio_path: Path) -> str | None:
    """Transcribe audio via Whisper API."""
    try:
        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru",
            )
        return result.text
    except Exception as e:
        print(f"[transcriber] Transcription failed for {audio_path}: {e}")
        return None


def process_call(record_url: str, lead_id: int) -> str | None:
    """Download + transcribe a call. Returns transcript text or None."""
    audio = download_audio(record_url, lead_id)
    if not audio:
        return None
    transcript = transcribe(audio)
    return transcript

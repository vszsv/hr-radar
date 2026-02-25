"""IMAP reader — fetch email bodies from Yandex mail."""
import imaplib
import email
import email.message
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
import html
from datetime import datetime, timedelta, timezone
from config import IMAP_HOST, IMAP_USER, IMAP_PASS


def _decode_header(raw: str) -> str:
    """Decode MIME-encoded header."""
    if not raw:
        return ""
    parts = decode_header(raw)
    result = []
    for data, charset in parts:
        if isinstance(data, bytes):
            result.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(data)
    return " ".join(result)


def _extract_text(msg: email.message.Message) -> str:
    """Extract plain text from email message."""
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                texts.append(payload.decode(charset, errors="replace"))
            elif ct == "text/html" and not texts:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                raw_html = payload.decode(charset, errors="replace")
                # Strip HTML tags
                clean = re.sub(r"<[^>]+>", " ", raw_html)
                clean = html.unescape(clean)
                clean = re.sub(r"\s+", " ", clean).strip()
                texts.append(clean)
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
            if ct == "text/html":
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = html.unescape(raw)
                raw = re.sub(r"\s+", " ", raw).strip()
            texts.append(raw)
    return "\n".join(texts)[:5000]  # Cap at 5000 chars


def _normalize(s: str) -> str:
    """Normalize string for fuzzy matching."""
    # Remove RE:/FW:/Fwd: prefixes, backslashes, extra spaces
    s = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s.lower().strip(), flags=re.IGNORECASE)
    s = re.sub(r"\\+", " ", s)
    s = re.sub(r"[/|\\]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _fuzzy_match(query: str, subject: str) -> bool:
    """Check if query and subject match (fuzzy)."""
    # Direct containment
    if query in subject or subject in query:
        return True
    # Partial containment (first 40 chars)
    if query[:40] in subject or subject[:40] in query:
        return True
    # Keyword overlap: at least 60% of significant words match
    stop = {"re", "fw", "fwd", "от", "на", "и", "в", "с", "из", "для", "по", "the", "a", "an"}
    q_words = set(w for w in query.split() if len(w) > 2 and w not in stop)
    s_words = set(w for w in subject.split() if len(w) > 2 and w not in stop)
    if q_words and s_words:
        overlap = len(q_words & s_words)
        min_set = min(len(q_words), len(s_words))
        if min_set > 0 and overlap / min_set >= 0.5:
            return True
    return False


def fetch_email_body(subject_query: str, sender_email: str = "", days_back: int = 30) -> str | None:
    """Find email by subject (fuzzy match) and return its body text.
    
    Searches INBOX and Spam folders.
    """
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST)
        conn.login(IMAP_USER, IMAP_PASS)
    except Exception as e:
        print(f"[imap] Login failed: {e}")
        return None

    since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%d-%b-%Y")
    norm_query = _normalize(subject_query)

    for folder in ["INBOX", "Spam"]:
        try:
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                continue

            # Strategy 1: search by sender email (most reliable)
            if sender_email:
                _, msg_nums = conn.search(None, f'(SINCE {since_date} FROM "{sender_email}")')
                ids = msg_nums[0].split() if msg_nums[0] else []
                # Check newest first, try subject match
                for msg_id in reversed(ids[-50:]):
                    _, header_data = conn.fetch(msg_id, "(BODY[HEADER.FIELDS (SUBJECT FROM)])")
                    if not header_data or not header_data[0]:
                        continue
                    raw_header = header_data[0][1] if isinstance(header_data[0], tuple) else b""
                    header_msg = email.message_from_bytes(raw_header)
                    
                    # Verify sender actually matches (IMAP FROM search is substring-based)
                    actual_from = _decode_header(header_msg.get("From", ""))
                    if sender_email.lower() not in actual_from.lower():
                        continue
                    
                    subj = _decode_header(header_msg.get("Subject", ""))
                    norm_subj = _normalize(subj)

                    if _fuzzy_match(norm_query, norm_subj):
                        # Exact-ish match — fetch body
                        _, full_data = conn.fetch(msg_id, "(RFC822)")
                        if full_data and full_data[0]:
                            raw_email = full_data[0][1] if isinstance(full_data[0], tuple) else b""
                            msg = email.message_from_bytes(raw_email)
                            body = _extract_text(msg)
                            conn.close()
                            conn.logout()
                            return body

                # No subject match from sender — skip (could be different thread)

            # Strategy 2: broad subject search (fallback)
            _, msg_nums = conn.search(None, f'(SINCE {since_date})')
            if not msg_nums[0]:
                continue

            ids = msg_nums[0].split()
            for msg_id in reversed(ids[-500:]):
                _, header_data = conn.fetch(msg_id, "(BODY[HEADER.FIELDS (SUBJECT FROM)])")
                if not header_data or not header_data[0]:
                    continue
                raw_header = header_data[0][1] if isinstance(header_data[0], tuple) else b""
                header_msg = email.message_from_bytes(raw_header)
                subj = _decode_header(header_msg.get("Subject", ""))
                norm_subj = _normalize(subj)

                if _fuzzy_match(norm_query, norm_subj):
                    _, full_data = conn.fetch(msg_id, "(RFC822)")
                    if full_data and full_data[0]:
                        raw_email = full_data[0][1] if isinstance(full_data[0], tuple) else b""
                        msg = email.message_from_bytes(raw_email)
                        body = _extract_text(msg)
                        conn.close()
                        conn.logout()
                        return body

        except Exception as e:
            print(f"[imap] Error in folder {folder}: {e}")
            continue

    try:
        conn.close()
    except:
        pass
    conn.logout()
    return None

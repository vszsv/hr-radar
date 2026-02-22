#!/usr/bin/env python3
import json
import os
import sys
from hh_session import build_session, is_session_alive, fetch_resume_details


def main():
    cookie_path = os.getenv("HH_COOKIES_PATH", "/opt/hr-radar/data/hh_cookies.json")
    test_url = os.getenv("HH_TEST_RESUME_URL", "")

    s = build_session(cookie_path)
    alive = is_session_alive(s)
    payload = {"alive": alive, "cookie_path": cookie_path}

    if test_url and alive:
        payload["test_resume"] = fetch_resume_details(s, test_url)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not alive:
        sys.exit(2)


if __name__ == "__main__":
    main()

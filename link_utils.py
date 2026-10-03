from __future__ import annotations

import re


_URL_PATTERN = re.compile(
    r"(?i)(?:https?://[^\s<>]+|www\.[^\s<>]+|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
    r"(?::\d{1,5})?(?:/[^\s<>]*)?)"
)


def extract_public_url(text: str) -> str | None:
    """Find the first HTTP(S)-style URL and normalize bare domains to HTTPS."""
    match = _URL_PATTERN.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,;!?)]}")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url

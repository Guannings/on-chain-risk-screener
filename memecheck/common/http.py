"""HTTP client — stdlib urllib for the synchronous scanner path.

Kept deliberately stdlib-only so the scanner lifecycle has no runtime deps.
The future async monitor will use httpx in its own module.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

UA: dict[str, str] = {"User-Agent": "memecheck/1.0"}


def get_json(url: str, timeout: int = 15) -> dict[str, Any]:
    """GET a URL and return parsed JSON, or {'_error': ...} on failure."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code} for {url}"}
    except Exception as e:  # noqa: BLE001 — network is wild, swallow + report
        return {"_error": f"{type(e).__name__}: {e} ({url})"}

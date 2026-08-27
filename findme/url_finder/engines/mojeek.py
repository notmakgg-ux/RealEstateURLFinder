"""Mojeek search engine adapter — uses Mojeek's official Search API.

Requires a Mojeek API key (free trial available, paid plans from £2/CPM).
Set MOJEEK_API_KEY in .env or environment to enable.

Without a key, the engine returns empty results with a warning.
"""

import os
import time
import random
from typing import Any

import requests

from engines.base import BaseEngine


class MojeekEngine(BaseEngine):
    """Search via Mojeek's official Search API."""

    name = "mojeek"

    API_URL = "https://api.mojeek.com/search"

    def __init__(self):
        self.api_key = os.getenv("MOJEEK_API_KEY", "")
        if not self.api_key:
            print("  [Mojeek] WARNING: No MOJEEK_API_KEY set. Mojeek results will be empty.")
            print("  [Mojeek] Get a key at: https://www.mojeek.com/services/search/web-search-api/")

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        if not self.api_key:
            return []

        for attempt in range(3):
            try:
                params = {
                    "api_key": self.api_key,
                    "q": query,
                    "num": min(max_results, 10),  # Mojeek API max per request
                    "fmt": "json",
                }
                resp = requests.get(self.API_URL, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()

                results = []
                for item in data.get("results", [])[:max_results]:
                    url = item.get("url", "")
                    title = item.get("title", "")
                    snippet = item.get("desc", "")[:200]
                    if url and url.startswith("http"):
                        results.append({
                            "url": url,
                            "title": title,
                            "snippet": snippet,
                        })

                return results

            except requests.exceptions.HTTPError as e:
                if resp.status_code == 401:
                    print(f"  [Mojeek] Invalid API key. Check MOJEEK_API_KEY.")
                    return []
                if resp.status_code == 429:
                    wait = (attempt + 1) * 5 + random.uniform(0, 3)
                    print(f"  [Mojeek] Rate limited. Waiting {wait:.0f}s...")
                    time.sleep(wait)
                else:
                    if attempt < 2:
                        time.sleep((attempt + 1) * 2 + random.uniform(0, 1))
                    else:
                        print(f"  [Mojeek] Failed after 3 attempts: {e}")
                        return []
            except Exception as e:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2 + random.uniform(0, 1))
                else:
                    print(f"  [Mojeek] Failed after 3 attempts: {e}")
                    return []

        return []

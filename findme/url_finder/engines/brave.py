"""Brave Search engine adapter using requests + BeautifulSoup."""

import time
import random
from typing import Any

import requests
from bs4 import BeautifulSoup

from engines.base import BaseEngine


class BraveEngine(BaseEngine):
    """Search via Brave Search using requests."""

    name = "brave"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                url = f"https://search.brave.com/search?q={query.replace(' ', '+')}"
                resp = requests.get(url, headers=self.HEADERS, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                results = []
                # Brave result snippets
                for snippet in soup.select("div.snippet, div[data-type=web]"):
                    a = snippet.select_one("a.result-header, a[href]")
                    desc = snippet.select_one(".snippet-description, .snippet-content")
                    if a:
                        href = a.get("href", "")
                        title = a.get_text(strip=True)
                        snippet_text = desc.get_text(strip=True)[:200] if desc else ""

                        if href.startswith("http") and "search.brave.com" not in href:
                            results.append({
                                "url": href,
                                "title": title,
                                "snippet": snippet_text,
                            })

                # Fallback: grab external links
                if not results:
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        text = a.get_text(strip=True)
                        if (href.startswith("http")
                                and "search.brave.com" not in href
                                and len(text) > 5):
                            results.append({
                                "url": href,
                                "title": text,
                                "snippet": "",
                            })

                return results[:max_results]

            except Exception as e:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2 + random.uniform(0, 1))
                else:
                    print(f"    Brave failed: {e}")
                    return []

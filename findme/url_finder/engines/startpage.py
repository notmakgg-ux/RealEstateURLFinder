"""Startpage search engine adapter using requests + BeautifulSoup."""

import time
import random
from typing import Any

import requests
from bs4 import BeautifulSoup

from engines.base import BaseEngine


class StartpageEngine(BaseEngine):
    """Search via Startpage using requests (lightweight, no browser needed)."""

    name = "startpage"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                url = f"https://www.startpage.com/do/search?q={query.replace(' ', '+')}"
                resp = requests.get(url, headers=self.HEADERS, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                results = []
                # Startpage wraps results in various div structures
                # Try multiple selectors
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    title = a.get_text(strip=True)

                    # Skip startpage internal links
                    if "startpage.com" in href:
                        continue
                    # Skip very short text (navigation links)
                    if len(title) < 5:
                        continue
                    # Must be an actual URL
                    if not href.startswith("http"):
                        continue

                    # Look for nearby snippet text
                    snippet = ""
                    parent = a.find_parent(["div", "li"])
                    if parent:
                        p = parent.find("p", class_=lambda c: c and "desc" in str(c).lower() if c else False)
                        if p:
                            snippet = p.get_text(strip=True)[:200]

                    results.append({
                        "url": href,
                        "title": title,
                        "snippet": snippet,
                    })

                return results[:max_results]

            except Exception as e:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2 + random.uniform(0, 1))
                else:
                    print(f"    Startpage failed: {e}")
                    return []

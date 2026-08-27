"""Bing search engine adapter."""

import time
import random
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

from engines.base import BaseEngine


class BingEngine(BaseEngine):
    """Search via Bing HTML scraping."""

    name = "bing"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _decode_bing_url(self, href: str) -> str:
        """Decode Bing's tracking redirects."""
        if "bing.com" in href:
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            u_param = params.get("u", [None])[0]
            if u_param:
                try:
                    decoded = unquote(u_param)
                    if decoded.startswith("http"):
                        return decoded
                except Exception:
                    pass
        if "%25" in href:
            try:
                return unquote(href)
            except Exception:
                pass
        return href

    def search(self, query: str, max_results: int = 25) -> list[dict[str, Any]]:
        for attempt in range(3):
            try:
                url = f"https://www.bing.com/search?q={query.replace(' ', '+')}&count={max_results}"
                resp = requests.get(url, headers=self.HEADERS, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                results = []
                for li in soup.select("li.b_algo"):
                    a = li.find("a", href=True)
                    if not a:
                        continue

                    href = self._decode_bing_url(a["href"])
                    title = a.get_text(strip=True)

                    # Get snippet from the li text minus the anchor text
                    snippet_el = li.find("div", class_="b_caption")
                    snippet = ""
                    if snippet_el:
                        p = snippet_el.find("p")
                        if p:
                            snippet = p.get_text(strip=True)[:200]

                    if href.startswith("http"):
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
                    print(f"    Bing failed after 3 attempts: {e}")
                    return []

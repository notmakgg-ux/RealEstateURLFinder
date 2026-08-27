"""
Pipeline orchestrator — coordinates crawling, extraction, validation, scoring,
and output for each company.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from config import settings
from models.schemas import (
    CompanyResult, ExtractedEmail, CrawlStatus, PageType,
    NormalizedURL, CrawlURL,
)
from utils.urls import normalize_url, get_root_domain
from utils.priority import prioritize_urls, assign_priority, classify_page_type
from utils.link_discovery import extract_internal_links
from utils.deduplication import deduplicate_emails
from crawler.http_crawler import HTTPCrawler
from crawler.crawlee_orchestrator import EmailCrawleeOrchestrator
from crawler.playwright_crawler import render_page
from crawler.sitemap import discover_sitemap_urls
from extraction.email_extractor import extract_all_emails
from extraction.context_extractor import extract_person_context
from classification.page_classifier import classify_page
from classification.role_classifier import classify_person_role
from classification.contact_ranker import rank_contacts, select_best_contact
from validation.email_validator import validate_email_comprehensive
from scoring.confidence import score_all

logger = logging.getLogger(__name__)


async def process_companies(
    companies: list[dict],
    concurrency: int = 50,
    progress_callback=None,
) -> list[CompanyResult]:
    """
    Process all companies through the full pipeline.
    
    Args:
        companies: list of dicts with company_name, website
        concurrency: max concurrent crawls
        progress_callback: optional callback(current, total, status_message)
    
    Returns:
        list of CompanyResult objects
    """
    results = []
    total = len(companies)

    # Initialize Crawlee orchestrator (macro level)
    orchestrator = EmailCrawleeOrchestrator()

    # Process companies in batches to manage resources
    batch_size = min(concurrency, 10)
    
    for i in range(0, total, batch_size):
        batch = companies[i:i + batch_size]
        tasks = []
        
        for company in batch:
            task = asyncio.create_task(
                asyncio.wait_for(
                    _process_single_company(
                        company["company_name"],
                        company["website"],
                        company.get("extra_columns", {}),
                    ),
                    timeout=120,  # 2 min max per company
                )
            )
            tasks.append((company["company_name"], task))

        for name, task in tasks:
            try:
                result = await task
                results.append(result)
                status = f"✓ {name}: {result.emails_found} emails" if result.emails_found else f"✗ {name}: no emails"
                logger.info(status)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout processing {name}")
                results.append(CompanyResult(
                    company_name=name,
                    original_website=company.get("website", ""),
                    status=CrawlStatus.FAILED,
                    error_type="TimeoutError",
                    error_message="Company processing timed out after 120s",
                    stage_failed="pipeline",
                ))
            except Exception as e:
                logger.error(f"Failed to process {name}: {e}")
                results.append(CompanyResult(
                    company_name=name,
                    original_website=company.get("website", ""),
                    status=CrawlStatus.FAILED,
                    error_type=type(e).__name__,
                    error_message=str(e),
                    stage_failed="pipeline",
                ))

            if progress_callback:
                progress_callback(len(results), total, f"Processed {name}")

    # Log orchestrator stats
    stats = orchestrator.stats
    logger.info(f"Crawlee orchestrator stats: {stats}")
    await orchestrator.close()

    return results


async def _process_single_company(
    company_name: str,
    website: str,
    extra_columns: dict = None,
) -> CompanyResult:
    """Process a single company through the full pipeline."""
    start_time = time.time()

    result = CompanyResult(
        company_name=company_name,
        original_website=website,
        normalized_url=normalize_url(website),
        root_domain=get_root_domain(normalize_url(website)),
        started_at=datetime.now(),
        status=CrawlStatus.PROCESSING,
    )

    http_crawler = HTTPCrawler()

    try:
        base_url = result.normalized_url

        # === STAGE 1: Discover URLs ===
        logger.info(f"[{company_name}] Discovering URLs from {base_url}")

        # Fetch homepage
        homepage_resp = await http_crawler.fetch(base_url)
        if homepage_resp is None or homepage_resp.status_code >= 400:
            result.status = CrawlStatus.FAILED
            result.error_type = "fetch_error"
            result.error_message = f"Could not fetch homepage: HTTP {homepage_resp.status_code if homepage_resp else 'None'}"
            result.stage_failed = "homepage_fetch"
            return result

        homepage_html = homepage_resp.text

        # Extract links from homepage
        internal_links = extract_internal_links(homepage_html, base_url)
        logger.info(f"[{company_name}] Found {len(internal_links)} internal links from homepage")

        # Discover sitemap URLs
        sitemap_urls = []
        try:
            sitemap_urls = await discover_sitemap_urls(base_url, http_crawler._client or await http_crawler._get_client())
            logger.info(f"[{company_name}] Found {len(sitemap_urls)} URLs from sitemaps")
        except Exception as e:
            logger.debug(f"[{company_name}] Sitemap discovery failed: {e}")

        # Merge and prioritize all URLs
        all_urls = list(set(internal_links + sitemap_urls))
        prioritized = prioritize_urls(all_urls, base_url)
        result.pages_discovered = len(prioritized)

        logger.info(f"[{company_name}] Prioritized {len(prioritized)} URLs")

        # === STAGE 2: Crawl priority pages ===
        all_emails_raw = []
        pages_crawled = 0
        max_pages = settings.max_total_pages_per_domain

        # Limit crawl to max pages
        urls_to_crawl = prioritized[:max_pages]

        # Batch fetch pages
        url_list = [cu.url for cu in urls_to_crawl]
        
        # Fetch in small batches
        crawled_htmls: dict[str, str] = {}
        BATCH = 10
        for i in range(0, len(url_list), BATCH):
            batch_urls = url_list[i:i + BATCH]
            responses = await http_crawler.fetch_many(batch_urls)
            for url, resp in responses.items():
                if resp and resp.status_code < 400:
                    crawled_htmls[url] = resp.text
                    pages_crawled += 1

        logger.info(f"[{company_name}] Crawled {pages_crawled} pages via HTTP")

        # === STAGE 3: Extract emails from HTTP pages ===
        playwright_needed = []
        
        for crawl_url in urls_to_crawl:
            url = crawl_url.url
            html = crawled_htmls.get(url)
            if not html:
                continue

            page_type = classify_page(url, html)
            raw_emails = extract_all_emails(html, url, page_type.value)

            for raw in raw_emails:
                email_obj = ExtractedEmail(
                    email=raw["email"],
                    source_url=raw.get("source_url", url),
                    source_page_type=page_type,
                    extraction_method=raw.get("extraction_method", "regex"),
                    nearby_text=raw.get("nearby_text", ""),
                    html_context=raw.get("html_context", ""),
                    person_name=raw.get("person_name", ""),
                    job_title=raw.get("job_title", ""),
                )
                all_emails_raw.append(email_obj)

            # If high-priority page found no emails, mark for Playwright fallback
            if not raw_emails and crawl_url.priority >= 85:
                playwright_needed.append(url)

        logger.info(f"[{company_name}] HTTP extraction found {len(all_emails_raw)} raw emails")

        # === STAGE 4: Playwright fallback for empty high-priority pages ===
        if settings.enable_playwright_fallback and playwright_needed:
            logger.info(f"[{company_name}] Running Playwright fallback on {len(playwright_needed)} pages")
            result.playwright_used = True

            for url in playwright_needed[:5]:  # Limit Playwright usage
                try:
                    html = await render_page(url, wait_ms=3000)
                    if html:
                        page_type = classify_page(url, html)
                        raw_emails = extract_all_emails(html, url, page_type.value)
                        for raw in raw_emails:
                            email_obj = ExtractedEmail(
                                email=raw["email"],
                                source_url=raw.get("source_url", url),
                                source_page_type=page_type,
                                extraction_method=raw.get("extraction_method", "regex"),
                                nearby_text=raw.get("nearby_text", ""),
                                html_context=raw.get("html_context", ""),
                                person_name=raw.get("person_name", ""),
                                job_title=raw.get("job_title", ""),
                            )
                            all_emails_raw.append(email_obj)
                except Exception as e:
                    logger.debug(f"[{company_name}] Playwright fallback failed for {url}: {e}")

        # === STAGE 5: Context extraction ===
        logger.info(f"[{company_name}] Enriching {len(all_emails_raw)} emails with context")

        for email_obj in all_emails_raw:
            if not email_obj.person_name or not email_obj.job_title:
                # Try to get context from HTML
                html = crawled_htmls.get(email_obj.source_url, "")
                if html:
                    ctx = extract_person_context(html, email_obj.email)
                    if ctx.get("person_name") and not email_obj.person_name:
                        email_obj.person_name = ctx["person_name"]
                    if ctx.get("job_title") and not email_obj.job_title:
                        email_obj.job_title = ctx["job_title"]
                    if ctx.get("role") and not email_obj.role:
                        email_obj.role = ctx["role"]

            # Classify role if we have a title but no role
            if email_obj.job_title and not email_obj.role:
                role_info = classify_person_role(
                    email_obj.person_name, email_obj.job_title, email_obj.email
                )
                email_obj.role = role_info.get("role", email_obj.job_title)

        # === STAGE 6: Validation ===
        logger.info(f"[{company_name}] Validating {len(all_emails_raw)} emails")

        validated_emails = []
        for email_obj in all_emails_raw:
            validation = validate_email_comprehensive(
                email_obj.email, result.root_domain
            )
            email_obj.syntax_valid = validation["syntax_valid"]
            email_obj.mx_valid = validation["mx_valid"]
            email_obj.domain_match = validation["domain_match"]
            email_obj.is_disposable = validation["is_disposable_email"]

            if validation["overall_valid"]:
                validated_emails.append(email_obj)
            else:
                result.emails_rejected += 1
                logger.debug(
                    f"[{company_name}] Rejected {email_obj.email}: "
                    f"syntax={validation['syntax_valid']}, "
                    f"disposable={validation['is_disposable_email']}, "
                    f"noreply={validation['is_noreply_email']}"
                )

        # === STAGE 7: Deduplication ===
        deduped = deduplicate_emails(validated_emails)
        logger.info(f"[{company_name}] After dedup: {len(deduped)} unique emails")

        # === STAGE 8: Confidence scoring ===
        scored = score_all(deduped)

        # === STAGE 9: Contact ranking ===
        ranked = rank_contacts(scored)
        result.emails = ranked
        result.emails_found = len(ranked)

        # Select best contact
        best = select_best_contact(ranked)
        result.best_contact_email = best["best_contact_email"]
        result.best_contact_name = best["best_contact_name"]
        result.best_contact_role = best["best_contact_role"]
        result.best_contact_score = best["best_contact_score"]

        result.status = CrawlStatus.COMPLETED if ranked else CrawlStatus.NO_EMAIL_FOUND

    except Exception as e:
        logger.error(f"[{company_name}] Pipeline error: {e}")
        result.status = CrawlStatus.FAILED
        result.error_type = type(e).__name__
        result.error_message = str(e)
        result.stage_failed = "pipeline_error"
        import traceback
        traceback.print_exc()

    finally:
        await http_crawler.close()
        result.completed_at = datetime.now()
        result.duration_seconds = time.time() - start_time
        result.pages_crawled = pages_crawled

    return result

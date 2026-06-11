#!/usr/bin/env python3
"""
Poll Japanese job-board listing pages (Lancers.jp + CrowdWorks.jp) and post Discord webhooks when new jobs appear.

Configure with environment variables:

  JOB_BOARD_URLS       Optional. If set, **only** these space-separated URLs are monitored (full control of order and mix).
                         Webhooks must be set with ``DISCORD_WEBHOOK_URL`` / ``DISCORD_WEBHOOK_URLS`` (same count as URLs).

  Lancers.jp (ignored when JOB_BOARD_URLS is set):

  LANCERS_URLS                    Search URLs (default: system + web).
  LANCERS_DISCORD_WEBHOOK_URLS    Space-separated Discord webhooks, **one per LANCERS_URLS URL** (same order).

  CrowdWorks.jp (ignored when JOB_BOARD_URLS is set):

  CROWDWORKS_URLS                     Search URLs. Default when this key is **absent**: category **226** (システム) + **230** (Web) with ``order=new``.
                                          Set ``CROWDWORKS_URLS`` to an empty value to disable CrowdWorks listings.
  CROWDWORKS_DISCORD_WEBHOOK_URLS     Space-separated Discord webhooks, **one per CROWDWORKS_URLS URL** (same order).
  CROWDWORKS_FETCH_EMPLOYER_PROFILE     If falsy only: skip GET ``/public/employers/{id}`` enrichment (default on). Listing JSON exposes sparse ``client``; stars and「募集実績・完了・契約・完了率」derive from employer profile payloads.
  CROWDWORKS_PROFILE_CACHE_SECONDS     Seconds to memoize parsed employer payloads per user id (default ``3600``). Set ``0`` to disable caching.

  LANCERS_FETCH_CLIENT_PROFILE        If falsy only: skip GET ``/client/{nickname}`` enrichment (default on). Search cards omit 発注/評価 for many rows;プロフィール先のヘッダ表から発注数・発注率・フィードバック内訳・継続ランサーを補う。
  LANCERS_CLIENT_PROFILE_CACHE_SECONDS  Memoize parsed client payloads per `/client/` path (default ``3600``). Set ``0`` to disable caching.

  DASHBOARD_INGEST            If "1"/"true"/"yes", POST poll results to the Job Hunter dashboard API.
  DASHBOARD_INGEST_URL        Base URL of the dashboard **Next.js origin only** — no trailing path (e.g. ``http://127.0.0.1:3000`` or ``https://your-app.vercel.app``). POST target is ``{base}/api/internal/ingest``. If Next uses ``basePath`` (subpath deployment), include that path in this URL (e.g. ``https://host/jobhunter``). Requires ``DASHBOARD_INGEST_SECRET``.
  DASHBOARD_INGEST_SECRET     Shared secret — must match ``DASHBOARD_INGEST_SECRET`` in dashboard ``.env``.
  DASHBOARD_RECORD_SCRAPES    If "1", send scrape rows every poll even when no new jobs (can be chatty).
  MONITOR_PARSER_VERSION      Optional string stored as ``parser_version`` on sources (default: ``monitor.py``).

  Discord notification layout:

    • Recommended: ``LANCERS_DISCORD_WEBHOOK_URLS`` and ``CROWDWORKS_DISCORD_WEBHOOK_URLS`` as soon as **either**
      platform-specific variable appears in the environment (do not rely on legacy ``DISCORD_WEBHOOK_URLS`` in that mode).
    • Legacy combined list ``DISCORD_WEBHOOK_URLS``: one webhook per URL in order (**Lancers first**, **CrowdWorks second**).
      Used only when **neither** platform-specific webhook variable exists.
    • ``DISCORD_WEBHOOK_URL``: one channel for **all** URLs.

  COPY_TO_CLIPBOARD          If enabled (default 1), writes plain-text 「依頼詳細」をまとめた内容 to the OS clipboard whenever new jobs are notified
  POLL_INTERVAL_SECONDS Polling interval when running in daemon mode (default 180)
  STATE_PATH            JSON file storing seen job IDs **per category** under ``known_ids_by_category``
                           (e.g. ``system``, ``web``, ``cw_226``, ``cw_230``).
  HTTP_TIMEOUT_SECONDS  Request timeout (default 30)
  NOTIFY_ON_FIRST_RUN   If "1"/"true"/"yes", notify for all jobs on the first poll instead of only seeding state
  DISCORD_POST_DELAY_SECONDS   Pause between webhook requests when sending batches (default 1.25)
  DISCORD_MAX_EMBEDS_PER_MESSAGE  Up to 10 embeds per POST (default 10; lower if payloads are huge)
  DISCORD_WEBHOOK_MAX_RETRIES     Max retries on HTTP 429 (default 24)
  DISCORD_RATE_LIMIT_MARGIN_SECONDS Extra seconds added to Discord's retry_after (default 0.15)
  COPY_TO_CLIPBOARD          If enabled (default 1), writes plain-text 「依頼詳細」をまとめた内容 to the OS clipboard whenever new jobs are notified
  CLIPBOARD_FETCH_WORK_DETAIL Fetch each job detail page to copy full「依頼概要」(default 1). If off, uses only the teaser from the listing page.
  DISCORD_NOTIFY_ON_START     Post a Discord message when the monitor process starts (default 1).
  PREVIEW_HTTP_BIND           Bind address for ``--preview`` (default 127.0.0.1 — use SSH port forward from a VPS).
  PREVIEW_HTTP_PORT           Port for ``--preview`` (default 8790).

Compliance: Requests use a descriptive User-Agent. Poll at considerate intervals.

Usage:
  DISCORD_WEBHOOK_URL=... python monitor.py               # loops forever (or rely on `.env`)
  DISCORD_WEBHOOK_URL=... python monitor.py --once       # single poll (cron)
  python monitor.py --reset-state                        # clear seen-job state and exit
  python monitor.py --preview                            # fetch once, serve structured data at http://127.0.0.1:8790/ and /json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import html
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import Any
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


def load_env_file_if_available() -> None:
    """Populate os.environ from a local `.env` when python-dotenv is installed.

    Uses override=True so values in `.env` replace stale/partial variables already
    exported in the shell (a common cause of truncated DISCORD_WEBHOOK_URL).
    To force the shell value instead, run: `env -u DISCORD_WEBHOOK_URL python monitor.py`
    after unsetting, or edit `.env` to match.
    """
    try:
        from dotenv import load_dotenv  # type: ignore[import-untyped]
    except ImportError:
        return
    load_dotenv(override=True)


LOG = logging.getLogger("lancers_monitor")

DEFAULT_LANCERS_URLS = (
    "https://www.lancers.jp/work/search/system?open=1&ref=header_menu",
    "https://www.lancers.jp/work/search/web?open=1&ref=header_menu",
)

DEFAULT_CROWDWORKS_URLS = (
    "https://crowdworks.jp/public/jobs/search?category_id=226&order=new",
    "https://crowdworks.jp/public/jobs/search?category_id=230&order=new",
)

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 JobHunterMonitor/1.0"
)

WORK_ID_RE = re.compile(r"/work/detail/(\d+)")
LANCERS_CLIENT_PROFILE_PATH_RE = re.compile(r"(?:/+|^)/client/([^/?#]+)/?", re.IGNORECASE)
REQUEST_DETAIL_TERM_MARKERS = ("依頼概要", "募集内容", "仕事の内容", "依頼内容", "仕事詳細")
DISCORD_WEBHOOK_EXECUTE_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/(\d+)/([^/]+)$")
DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "canary.discord.com",
        "ptb.discord.com",
        "discordapp.com",
    }
)

STATE_SCHEMA_VERSION = 2


@dataclass
class JobListing:
    work_id: str
    title: str
    budget_text: str
    detail_url: str
    client_name: str
    client_profile_url: str | None
    client_extras: str
    source_listing_url: str
    listing_summary: str
    client_avatar_url: str | None = None
    client_orders: str | None = None
    client_rating: float | None = None
    # 事業主体（個人/法人）推定の補助シグナル（CrowdWorks 雇用者プロフィール由来）。
    client_is_official_account: bool | None = None
    client_is_certified_employer: bool | None = None
    client_identity_verified: bool | None = None

    def client_summary(self) -> str:
        parts = []
        name = self.client_name.strip()
        if name:
            parts.append(name)
        if self.client_profile_url:
            ch = self.client_profile_url.strip()
            if ch.startswith("http://") or ch.startswith("https://"):
                parts.append(ch)
            else:
                du = urlparse(self.detail_url)
                base = f"{du.scheme}://{du.netloc}" if du.scheme and du.netloc else "https://www.lancers.jp"
                parts.append(urljoin(base + "/", ch.lstrip("/")))
        extras = self.client_extras.strip()
        if extras:
            parts.append(extras)
        return "\n".join(parts) if parts else "(発注者情報なし)"


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    s = v.strip().lower()
    if s in ("0", "false", "no", "off", ""):
        return False
    return s in ("1", "true", "yes", "on")


def normalize_discord_webhook_url(raw: str) -> str:
    u = raw.strip().strip("\ufeff").replace("\r", "").strip()
    if len(u) >= 2 and u[0] == u[-1] and u[0] in "\"'":
        u = u[1:-1].strip()
    return u.rstrip()


def webhook_url_diagnostic(url: str) -> str:
    """Non-secret hints for troubleshooting invalid webhook URLs."""
    if not url:
        return "(empty)"
    p = urlparse(url)
    segs = [s for s in p.path.strip("/").split("/") if s]
    prefix = (p.scheme + "://" + (p.netloc or ""))[:44]
    return f"len={len(url)} scheme={p.scheme!r} host={p.hostname!r} path_segments={len(segs)} prefix={prefix!r}"


def validate_discord_webhook_execute_url(url: str) -> None:
    """Raise ValueError unless URL looks like a standard incoming-webhook POST target."""
    if not url:
        raise ValueError("Webhook URL is empty.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Webhook URL must start with https://")
    host = (parsed.hostname or "").lower()
    if host not in DISCORD_WEBHOOK_HOSTS:
        raise ValueError(
            f"Webhook URL has unexpected host {parsed.hostname!r}. "
            "Use the URL from Discord: Server Settings → Integrations → Webhooks → Copy Webhook URL."
        )
    path = parsed.path.rstrip("/") or "/"
    if not DISCORD_WEBHOOK_EXECUTE_PATH.match(path):
        raise ValueError(
            "Webhook URL must look like "
            "https://discord.com/api/webhooks/<webhook_id>/<token> — without extra path segments "
            "such as /slack. If the token is missing or cut off, Discord may return HTTP 405."
        )


def _split_ws_urls(raw: str) -> list[str]:
    return [u for u in (x.strip() for x in raw.split()) if u]


def _lancers_listing_urls_from_env() -> list[str]:
    raw = os.environ.get("LANCERS_URLS", "").strip()
    return _split_ws_urls(raw) if raw else list(DEFAULT_LANCERS_URLS)


def _crowdworks_listing_urls_from_env() -> list[str]:
    if "CROWDWORKS_URLS" in os.environ:
        cw_raw = os.environ.get("CROWDWORKS_URLS", "").strip()
        return _split_ws_urls(cw_raw) if cw_raw else []
    return list(DEFAULT_CROWDWORKS_URLS)


def load_urls_env() -> list[str]:
    merged = os.environ.get("JOB_BOARD_URLS", "").strip()
    if merged:
        return _split_ws_urls(merged)
    return _lancers_listing_urls_from_env() + _crowdworks_listing_urls_from_env()


def _normalize_webhook_list(raw: str) -> list[str]:
    hooks = [normalize_discord_webhook_url(h) for h in _split_ws_urls(raw)]
    return [h for h in hooks if h]


def _platform_discord_webhook_keys_used() -> bool:
    return "LANCERS_DISCORD_WEBHOOK_URLS" in os.environ or "CROWDWORKS_DISCORD_WEBHOOK_URLS" in os.environ


def _assignments_from_platform_webhooks(lan_u: list[str], cw_u: list[str]) -> dict[str, str]:
    """Build listing URL → webhook using LANCERS_DISCORD_WEBHOOK_URLS / CROWDWORKS_DISCORD_WEBHOOK_URLS."""
    lan_key = "LANCERS_DISCORD_WEBHOOK_URLS"
    cw_key = "CROWDWORKS_DISCORD_WEBHOOK_URLS"
    assignments: dict[str, str] = {}

    if lan_u:
        if lan_key not in os.environ:
            raise ValueError(
                f"Set `{lan_key}` ({len(lan_u)} space-separated webhook(s), same order as Lancers URLs) "
                "or remove platform webhook keys and use DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URLS instead."
            )
        lan_h = _normalize_webhook_list(os.environ.get(lan_key, ""))
        if len(lan_u) != len(lan_h):
            raise ValueError(
                f"`{lan_key}` needs exactly one webhook per Lancers listing URL "
                f"({len(lan_u)} URL(s); found {len(lan_h)} webhook(s))."
            )
        assignments.update(zip(lan_u, lan_h, strict=True))
    else:
        if lan_key in os.environ and _normalize_webhook_list(os.environ.get(lan_key, "")):
            raise ValueError(f"`{lan_key}` defines webhooks but there are zero Lancers listing URLs.")

    if cw_u:
        if cw_key not in os.environ:
            raise ValueError(
                f"Set `{cw_key}` ({len(cw_u)} space-separated webhook(s), same order as CrowdWorks URLs) "
                "or remove platform webhook keys and use DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URLS instead."
            )
        cw_h = _normalize_webhook_list(os.environ.get(cw_key, ""))
        if len(cw_u) != len(cw_h):
            raise ValueError(
                f"`{cw_key}` needs exactly one webhook per CrowdWorks listing URL "
                f"({len(cw_u)} URL(s); found {len(cw_h)} webhook(s))."
            )
        assignments.update(zip(cw_u, cw_h, strict=True))
    else:
        if cw_key in os.environ and _normalize_webhook_list(os.environ.get(cw_key, "")):
            raise ValueError(f"`{cw_key}` defines webhooks but CrowdWorks is disabled / has no URLs.")

    return assignments


def listing_url_is_crowdworks(listing_url: str) -> bool:
    host = (urlparse(listing_url).hostname or "").lower()
    return host.endswith("crowdworks.jp")


def listing_category_label(listing_url: str) -> str:
    """Short label for Discord / clipboard headers (e.g. ``system``, ``web``, ``cw_226``)."""
    try:
        p = urlparse(listing_url)
        host = (p.hostname or "").lower()
        if host.endswith("crowdworks.jp") and "/public/jobs/search" in p.path:
            qs = parse_qs(p.query)
            cat = (qs.get("category_id") or [""])[0].strip()
            if cat.isdigit():
                return f"cw_{cat}"
            return "crowdworks_search"
        parts = p.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "work" and parts[1] == "search":
            tail = "/".join(parts[2:])
            return tail if tail else "search"
        return parts[-1] if parts else listing_url[:60]
    except Exception:
        return listing_url[:60]


#: Friendly display names for known category keys (Discord / clipboard headers only).
#: State bucketing still uses the raw key from ``listing_category_label`` (e.g. ``cw_311``).
CATEGORY_DISPLAY_LABELS = {
    "system": "システム",
    "web": "Web",
    "cw_226": "システム (CW)",
    "cw_230": "Web (CW)",
    "cw_311": "AI (CW)",
}


def listing_category_display_label(listing_url: str) -> str:
    """Human-friendly category label for headers (falls back to the raw key)."""
    key = listing_category_label(listing_url)
    return CATEGORY_DISPLAY_LABELS.get(key, key)


def monitor_category_buckets(urls: list[str]) -> list[str]:
    """Ordered unique category keys for ``urls`` (system vs web, etc.)."""
    keys: list[str] = []
    seen: set[str] = set()
    for u in urls:
        k = listing_category_label(u)
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _dashboard_ingest_enabled() -> bool:
    return env_bool("DASHBOARD_INGEST", False) and bool(
        os.environ.get("DASHBOARD_INGEST_URL", "").strip()
        and os.environ.get("DASHBOARD_INGEST_SECRET", "").strip()
    )


def _dashboard_record_scrapes_without_jobs() -> bool:
    return env_bool("DASHBOARD_RECORD_SCRAPES", False)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_export_safe(obj: object) -> object:
    """Recursively coerce values so json.dumps(..., allow_nan=False) and strict JSON parsers accept the payload."""

    if obj is None:
        return None
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        for k, v in obj.items():
            out[str(k)] = _json_export_safe(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_json_export_safe(v) for v in obj]
    return str(obj)


def _truncate_chars(s: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(s) <= max_chars:
        return s
    suf = "\n...[truncated]"
    n = max(0, max_chars - len(suf))
    return s[:n] + suf


def _platform_label_for_listing(listing_url: str) -> str:
    return "CrowdWorks" if listing_url_is_crowdworks(listing_url) else "Lancers"


def _scraping_type_for_listing(listing_url: str) -> str:
    return "HYBRID" if listing_url_is_crowdworks(listing_url) else "HTML_PARSE"


def push_dashboard_ingest(
    session: requests.Session,
    listing_webhooks: dict[str, str],
    urls: list[str],
    jobs_found_per_url: dict[str, int],
    listing_errors: dict[str, str | None],
    new_entries: list[JobListing],
    discord_by_detail_url: dict[str, tuple[str, bool, str | None]],
    poll_started_iso: str,
    poll_finished_iso: str,
) -> None:
    if not _dashboard_ingest_enabled():
        return
    base = os.environ.get("DASHBOARD_INGEST_URL", "").strip().rstrip("/")
    secret = os.environ.get("DASHBOARD_INGEST_SECRET", "").strip()
    endpoint = f"{base}/api/internal/ingest"
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))
    parser_ver = os.environ.get("MONITOR_PARSER_VERSION", "monitor.py")

    try:
        host = socket.gethostname()
    except OSError:
        host = ""

    listings_payload: list[dict] = []
    for u in urls:
        err_t = listing_errors.get(u)
        success = err_t is None
        listings_payload.append(
            {
                "listingUrl": u,
                "platform": _platform_label_for_listing(u),
                "scrapingType": _scraping_type_for_listing(u),
                "success": success,
                "jobsFound": int(jobs_found_per_url.get(u, 0)),
                "errorMessage": err_t,
            }
        )

    detected_payload: list[dict] = []
    for job in new_entries:
        wh, ok_del, d_err = discord_by_detail_url.get(
            job.detail_url,
            (listing_webhooks.get(job.source_listing_url, ""), False, None),
        )
        row: dict = {
            "listingUrl": job.source_listing_url,
            "categoryLabel": _truncate_chars(listing_category_label(job.source_listing_url), 250),
            "workId": _truncate_chars(job.work_id, 92),
            "title": _truncate_chars(job.title, 8000),
            "budget": _truncate_chars(job.budget_text, 8000),
            "clientName": _truncate_chars(job.client_name, 2000),
            "detailUrl": job.detail_url,
            "listingSummary": _truncate_chars(job.listing_summary or "", 120_000),
            "discordDelivered": ok_del,
            "discordError": _truncate_chars(d_err, 4000) if d_err else d_err,
            "raw": _json_export_safe(job_public_dict(job)),
        }
        if wh.strip():
            row["webhookUrl"] = wh.strip()
        detected_payload.append(row)

    if not detected_payload and not _dashboard_record_scrapes_without_jobs():
        return

    payload = {
        "pollStartedAt": poll_started_iso,
        "pollFinishedAt": poll_finished_iso,
        "workerHost": host[:250] if host else None,
        "pollingIntervalSeconds": interval,
        "parserVersion": parser_ver[:120],
        "listings": listings_payload,
        "detectedJobs": detected_payload,
    }
    payload_safe = _json_export_safe(payload)
    try:
        body = json.dumps(payload_safe, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        LOG.warning("Dashboard ingest: could not serialize payload: %s", e)
        return

    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": os.environ.get("HTTP_USER_AGENT", DEFAULT_UA).strip() or DEFAULT_UA,
    }
    try:
        r = session.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=45)
        if r.status_code >= 400:
            snippet = ((r.text or "").replace("\n", " ").strip())[:360]
            LOG.warning(
                "Dashboard ingest HTTP %s for %s — %s",
                r.status_code,
                endpoint,
                snippet or "(empty body)",
            )
            if r.status_code == 404:
                LOG.warning(
                    "Ingest 404 usually means DASHBOARD_INGEST_URL is not the Next app root that serves "
                    "`/api/internal/ingest` (wrong host, missing deploy, or app uses `basePath` / subpath without "
                    "including it in the base URL).",
                )
        else:
            LOG.info("Dashboard ingest OK (%d listing(s), %d new job row(s))", len(listings_payload), len(detected_payload))
    except requests.RequestException as e:
        LOG.warning("Dashboard ingest request failed: %s", e)


def load_listing_webhook_assignments(urls: list[str]) -> dict[str, str]:
    """Map each monitored listing URL → Discord webhook URL."""
    if os.environ.get("JOB_BOARD_URLS", "").strip():
        multi_raw = os.environ.get("DISCORD_WEBHOOK_URLS", "").strip()
        single_raw = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if _platform_discord_webhook_keys_used():
            raise ValueError(
                "Do not combine `JOB_BOARD_URLS` with `LANCERS_DISCORD_WEBHOOK_URLS` / "
                "`CROWDWORKS_DISCORD_WEBHOOK_URLS`. Use DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URLS matching "
                "the custom URL list instead."
            )
        if multi_raw:
            hooks = [normalize_discord_webhook_url(h) for h in multi_raw.split()]
            hooks = [h for h in hooks if h]
            if len(hooks) != len(urls):
                raise ValueError(
                    "`DISCORD_WEBHOOK_URLS` must list one webhook per `JOB_BOARD_URLS` entry "
                    f'({len(urls)} URLs; {len(hooks)} webhook(s)).'
                )
            return dict(zip(urls, hooks, strict=True))
        single = normalize_discord_webhook_url(single_raw)
        if single:
            return {u: single for u in urls}
        raise ValueError(
            "With JOB_BOARD_URLS set, configure DISCORD_WEBHOOK_URL (same channel for all URLs) "
            "or DISCORD_WEBHOOK_URLS with one webhook per URL in the same order."
        )

    lan_u = _lancers_listing_urls_from_env()
    cw_u = _crowdworks_listing_urls_from_env()
    if urls != lan_u + cw_u:
        raise RuntimeError("internal: listing URLs out of sync with env composition.")

    if _platform_discord_webhook_keys_used():
        return _assignments_from_platform_webhooks(lan_u, cw_u)

    multi_raw = os.environ.get("DISCORD_WEBHOOK_URLS", "").strip()
    single_raw = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if multi_raw:
        hooks = [normalize_discord_webhook_url(h) for h in multi_raw.split()]
        hooks = [h for h in hooks if h]
        if len(hooks) != len(urls):
            raise ValueError(
                "`DISCORD_WEBHOOK_URLS` must list exactly one webhook per listing URL "
                f"({len(urls)} URL(s): Lancers first, then CrowdWorks; received {len(hooks)} webhook(s)). "
                "Alternatively split by platform using `LANCERS_DISCORD_WEBHOOK_URLS` + `CROWDWORKS_DISCORD_WEBHOOK_URLS`."
            )
        return {urls[i]: hooks[i] for i in range(len(urls))}

    single = normalize_discord_webhook_url(single_raw)
    if not single:
        raise ValueError(
            "Set Discord webhooks via `DISCORD_WEBHOOK_URL` / `DISCORD_WEBHOOK_URLS`, or use platform-specific "
            "`LANCERS_DISCORD_WEBHOOK_URLS` plus `CROWDWORKS_DISCORD_WEBHOOK_URLS` (see module docstring)."
        )
    return {u: single for u in urls}


def fetch_html(session: requests.Session, url: str, timeout: float) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_budget(card: BeautifulSoup) -> str:
    node = card.select_one("span.p-search-job-media__price")
    if not node:
        return "(未取得)"
    return " ".join(node.get_text(separator=" ", strip=True).split())


def absolutize_lancers_asset(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return urljoin("https://www.lancers.jp/", u.lstrip("/"))


def parse_client(card: BeautifulSoup) -> tuple[str, str | None, str, str | None, str | None, float | None]:
    """Returns name, profile href, extras summary text, avatar URL, orders count text, rating float."""
    note = card.select_one("p.p-search-job-media__avatar-note a")
    name = ""
    href: str | None = None
    if note:
        name = note.get_text(strip=True)
        href = note.get("href")

    if not href:
        wrap = card.select_one('a.p-search-job-media__avatar-image-wrapper[href^="/client/"]')
        if wrap:
            href = wrap.get("href")
            img0 = wrap.select_one("img.c-avatar__image")
            if img0:
                name = (
                    (img0.get("alt") or "").strip()
                    or (img0.get("title") or "").strip()
                    or name
                )
            if not name:
                name = wrap.get_text(strip=True)

    img = card.select_one("div.p-search-job-media__avatar img.c-avatar__image")
    avatar_src = img.get("src") if img else None
    avatar_url = absolutize_lancers_asset(avatar_src)

    extras: list[str] = []
    orders: str | None = None
    rating: float | None = None
    for sub in card.select("span.p-search-job-media__avatar-subnote"):
        t = sub.get_text(separator=" ", strip=True)
        if t:
            extras.append(t)
        strong = sub.select_one("strong")
        num_txt = strong.get_text(strip=True) if strong else ""
        if not num_txt:
            continue
        if "発注" in t:
            orders = num_txt
        elif "評価" in t:
            try:
                rating = float(num_txt.replace(",", "."))
            except ValueError:
                rating = None

    img_latest = card.select_one("div.p-search-job__latest-media-thumbnail img.c-avatar__image")
    if img_latest:
        avatar_src = img_latest.get("src")
        avatar_url = absolutize_lancers_asset(avatar_src)

    if not href:
        alt = card.select_one('a[href^="/client/"]')
        if alt:
            href = alt.get("href")
            if not name:
                name = alt.get_text(strip=True)

    return name, href, " / ".join(extras) if extras else "", avatar_url, orders, rating


def parse_listing_summary_text(card: BeautifulSoup) -> str:
    """Snippet shown on the search result card (一覧の説明文)."""
    for desc in card.select("div.c-media__description"):
        if desc.select_one("ul.p-search-job-media__tag-lists"):
            continue
        t = desc.get_text(separator="\n", strip=True)
        if t:
            return t
    return ""


def extract_request_details_from_work_detail_html(page_html: str) -> str:
    """Full「依頼概要」(or similar) section from https://www.lancers.jp/work/detail/{id}."""
    soup = BeautifulSoup(page_html, "html.parser")
    for dl in soup.select("dl.c-definition-list"):
        dt = dl.select_one("dt.c-definition-list__term")
        dd = dl.select_one("dd.c-definition-list__description")
        if not dt or not dd:
            continue
        term = " ".join(dt.get_text(separator=" ", strip=True).split())
        if any(marker in term for marker in REQUEST_DETAIL_TERM_MARKERS):
            body = dd.get_text(separator="\n", strip=True)
            if body:
                return body
    return ""


def extract_crowdworks_detail_description(page_html: str) -> str:
    """Job body from CrowdWorks detail page (JobPosting JSON-LD ``description``, HTML-encoded)."""
    soup = BeautifulSoup(page_html, "html.parser")
    for sc in soup.select('script[type="application/ld+json"]'):
        raw = (sc.string or sc.get_text() or "").strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(d, list):
            d = d[0] if d else {}
        if not isinstance(d, dict):
            continue
        if d.get("@type") != "JobPosting":
            continue
        desc = d.get("description")
        if not desc or not isinstance(desc, str):
            continue
        plain = BeautifulSoup(html.unescape(desc), "html.parser").get_text(
            separator="\n", strip=True
        )
        if plain:
            return plain
    return ""


def extract_job_detail_for_clipboard(detail_html: str, detail_url: str) -> str:
    host = (urlparse(detail_url).hostname or "").lower()
    if host.endswith("crowdworks.jp"):
        return extract_crowdworks_detail_description(detail_html)
    return extract_request_details_from_work_detail_html(detail_html)


def _cw_format_yen(amount: float | int | None) -> str:
    if amount is None:
        return "—"
    try:
        n = int(amount)
        return f"{n:,}"
    except (TypeError, ValueError):
        return str(amount)


def crowdworks_payment_summary(payment: dict | None) -> str:
    if not isinstance(payment, dict):
        return "(未取得)"
    parts: list[str] = []

    hw = payment.get("hourly_payment")
    if isinstance(hw, dict):
        mn = hw.get("min_hourly_wage")
        mx = hw.get("max_hourly_wage")
        if mn is not None or mx is not None:
            parts.append(
                f"時給 {_cw_format_yen(mn)}〜{_cw_format_yen(mx)} 円"
            )

    fp = payment.get("fixed_price_payment")
    if isinstance(fp, dict):
        mn = fp.get("min_budget")
        mx = fp.get("max_budget")
        if mn is not None or mx is not None:
            parts.append(f"固定 {_cw_format_yen(mn)}〜{_cw_format_yen(mx)} 円")

    return " · ".join(parts) if parts else "予算（一覧）: 要確認 / 応相談"


def absolutize_crowdworks_asset(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    return urljoin("https://crowdworks.jp/", u.lstrip("/"))


def _cw_dict(*candidates: object) -> dict | None:
    for c in candidates:
        if isinstance(c, dict) and c:
            return c
    return None


def _cw_project_entry(row: dict) -> dict:
    """Resolve ``project_entry`` across known CrowdWorks search JSON shapes."""
    entry = row.get("entry")
    if isinstance(entry, dict):
        pe = entry.get("project_entry")
        if isinstance(pe, dict):
            return pe
    pe2 = row.get("project_entry")
    if isinstance(pe2, dict):
        return pe2
    return {}


def _cw_client_display_name(client: dict, row: dict) -> str:
    for key in ("username", "display_name", "name", "nickname", "screen_name", "company_name"):
        v = client.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    emp = _cw_dict(row.get("employer"))
    if emp:
        for key in ("display_name", "username", "name", "company_name"):
            v = emp.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _cw_client_uid(client: dict, row: dict) -> int | None:
    for obj in (client, _cw_dict(row.get("employer")), row):
        if not isinstance(obj, dict):
            continue
        for key in ("user_id", "id", "employer_id", "client_id"):
            v = obj.get(key)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _cw_rating_from_payload(client: dict, row: dict, jo: dict) -> float | None:
    """Average rating on ~0–5 scale from search / job-offer JSON (field names vary by CW version)."""
    rating_keys = (
        "average_rating",
        "average_score",
        "rating",
        "evaluation_rating",
        "employer_rating",
        "review_score",
        "client_rating",
        "star_rating",
    )
    for obj in (
        client,
        jo,
        _cw_dict(row.get("employer")),
        row,
        _cw_dict(row.get("client_stats")),
    ):
        if not isinstance(obj, dict):
            continue
        for k in rating_keys:
            v = obj.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                f = float(v)
                if 0.0 <= f <= 5.0:
                    return f
            if isinstance(v, str) and v.strip():
                try:
                    f = float(v.strip().replace(",", "."))
                    if 0.0 <= f <= 5.0:
                        return f
                except ValueError:
                    pass
    return None


def _cw_orders_text(row: dict, project_entry: dict, client: dict) -> str | None:
    num_c = project_entry.get("num_contracts")
    if num_c is not None:
        return str(int(num_c)) if isinstance(num_c, (int, float)) else str(num_c)
    for obj in (project_entry, client, row, _cw_dict(row.get("employer"))):
        if not isinstance(obj, dict):
            continue
        for key in (
            "num_contracts",
            "contract_count",
            "total_contracts",
            "completed_contracts",
            "job_offer_achievement_count",
        ):
            v = obj.get(key)
            if v is None:
                continue
            try:
                return str(int(v))
            except (TypeError, ValueError):
                if isinstance(v, str) and v.strip().isdigit():
                    return v.strip()
    return None


def _cw_completion_rate_pct(row: dict, client: dict) -> int | None:
    """Optional completion rate (0–100) for UI / rawData."""
    for obj in (client, row, _cw_dict(row.get("employer"))):
        if not isinstance(obj, dict):
            continue
        for key in ("project_completion_rate", "completion_rate", "contract_completion_rate"):
            v = obj.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                n = int(round(float(v)))
                if 0 <= n <= 100:
                    return n
            if isinstance(v, str) and v.strip().replace(".", "").isdigit():
                try:
                    n = int(round(float(v.strip())))
                    if 0 <= n <= 100:
                        return n
                except ValueError:
                    pass
    return None


def _lancers_metrics_regex_fallback(card: Tag) -> tuple[str | None, float | None]:
    """When subnote selectors miss (markup change), recover 発注 / 評価 from card text."""
    text = card.get_text(" ", strip=True)
    if not text:
        return None, None
    orders: str | None = None
    rating: float | None = None
    mo = re.search(r"発注\s*([\d,]+)", text)
    if mo:
        orders = mo.group(1).replace(",", "")
    mr = re.search(r"評価\s*([\d.,]+)", text)
    if mr:
        try:
            rating = float(mr.group(1).replace(",", "."))
        except ValueError:
            rating = None
    return orders, rating


CW_EMPLOYER_URL_ID_RE = re.compile(r"(?:/+|^)(?:public/)?employers/(\d+)(?:[^\d]|$)", re.IGNORECASE)
_CROWDWORKS_EMPLOYER_STATS_CACHE_V1: dict[int, tuple[float, dict[str, Any]]] = {}

_LANCERS_CLIENT_STATS_CACHE_V1: dict[str, tuple[float, dict[str, Any] | None]] = {}


def crowdworks_extract_employer_user_id(profile_href: str | None) -> int | None:
    """Parse employer user id from a relative `/public/employers/{id}` or absolute CrowdWorks URL."""
    if not profile_href or not isinstance(profile_href, str):
        return None
    s = profile_href.strip()
    m = CW_EMPLOYER_URL_ID_RE.search(s.replace("\\", "/"))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def job_detail_url_is_crowdworks(detail_url: str) -> bool:
    host = (urlparse(detail_url).hostname or "").lower()
    return host.endswith("crowdworks.jp")


def _crowdworks_profile_cache_ttl_s() -> float:
    raw = os.environ.get("CROWDWORKS_PROFILE_CACHE_SECONDS", "3600").strip()
    try:
        n = float(raw)
        return max(60.0, n) if n > 0 else 0.0
    except ValueError:
        return 3600.0


def parse_crowdworks_employer_profile_stats(page_html: str) -> dict[str, Any] | None:
    """
    Extract aggregate client metrics from CrowdWorks ``/public/employers/{id}`` HTML payload.

    The search-result ``#vue-container`` JSON exposes only sparse ``client``; the employer page embeds
    ``employer_profile_json.employer_user`` with ratings (総合評価), recruitment history
    ``job_offer_achievement_count``, and ``project_finished_data`` for 完了/契約/完了率.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    vc = soup.select_one("#vue-container")
    if not vc:
        return None
    raw_data = vc.get("data") or ""
    try:
        payload = json.loads(html.unescape(raw_data))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    prof = payload.get("employer_profile_json")
    eu = None
    if isinstance(prof, dict):
        eu = prof.get("employer_user")
    if not isinstance(eu, dict):
        eu = {}

    fb = eu.get("feedback")
    fb = fb if isinstance(fb, dict) else {}
    pfd = eu.get("project_finished_data")
    pfd = pfd if isinstance(pfd, dict) else {}

    def _clamp_star(v: float) -> float:
        return float(max(0.0, min(5.0, v)))

    rating: float | None = None
    sc_raw = fb.get("average_score")
    if isinstance(sc_raw, bool):
        sc_raw = None
    elif isinstance(sc_raw, (int, float)):
        try:
            fv = float(sc_raw)
            if not math.isnan(fv):
                rating = _clamp_star(fv)
        except (TypeError, ValueError):
            pass
    elif isinstance(sc_raw, str) and sc_raw.strip():
        try:
            fv = float(sc_raw.strip().replace(",", "."))
            if not math.isnan(fv):
                rating = _clamp_star(fv)
        except ValueError:
            pass

    def _positive_int(raw: Any) -> int | None:
        if isinstance(raw, bool) or raw is None:
            return None
        if isinstance(raw, int):
            return raw if raw >= 0 else None
        if isinstance(raw, float) and math.isfinite(raw):
            ir = int(raw)
            return ir if ir >= 0 and abs(float(ir) - raw) < 1e-6 else None
        if isinstance(raw, str) and raw.strip().replace(",", "").replace(".", "").isdigit():
            try:
                return int(round(float(raw.strip().replace(",", ""))))
            except ValueError:
                return None
        return None

    n_ratings = _positive_int(fb.get("total_count"))
    if rating is not None:
        # No substantive reviews → treat as unrated despite placeholder scores.
        if rating <= 0 or (n_ratings is not None and n_ratings <= 0):
            rating = None

    achievement = _positive_int(eu.get("job_offer_achievement_count"))
    # CrowdWorks ``project_finished_data`` uses English keys; the JP 「完了／契約」pair lines up
    # with these counts so 「完了」≤「契約」 in typical data (not ``finished``⇒完了 literally).
    total_acceptance_count = _positive_int(pfd.get("total_acceptance_count"))
    total_finished_count = _positive_int(pfd.get("total_finished_count"))
    cmp_raw = pfd.get("rate")
    completion_pct: int | None = None
    if isinstance(cmp_raw, (int, float)) and not isinstance(cmp_raw, bool) and math.isfinite(float(cmp_raw)):
        completion_pct = int(round(float(cmp_raw)))
        if completion_pct < 0 or completion_pct > 100:
            completion_pct = None
    elif isinstance(cmp_raw, str) and cmp_raw.strip().replace(".", "").isdigit():
        try:
            n = int(round(float(cmp_raw.strip())))
            if 0 <= n <= 100:
                completion_pct = n
        except ValueError:
            completion_pct = None

    dn = eu.get("display_name")
    display_name = dn.strip() if isinstance(dn, str) else None

    img = eu.get("profile_image_src")
    profile_image_src = img.strip() if isinstance(img, str) and img.strip() else None

    # 事業主体（個人/法人）推定の補助シグナル。公開プロフィールに法人/個人の確定値は無いため、
    # 公式アカウント・認定クライアント・本人確認の各フラグを推定材料として保存する。
    def _opt_bool(raw: Any) -> bool | None:
        return raw if isinstance(raw, bool) else None

    is_official_account = _opt_bool(eu.get("is_official_account"))
    is_certified_employer = _opt_bool(eu.get("is_certified_employer"))
    identity_verified = _opt_bool(eu.get("identity_verified"))

    if (
        rating is None
        and achievement is None
        and total_acceptance_count is None
        and total_finished_count is None
        and completion_pct is None
        and not display_name
        and not profile_image_src
        and is_official_account is None
        and is_certified_employer is None
        and identity_verified is None
    ):
        return None

    out: dict[str, Any] = {
        "average_rating": rating,
        "job_offer_achievement_count": achievement,
        "total_acceptance_count": total_acceptance_count,
        "total_finished_count": total_finished_count,
        "completion_rate_pct": completion_pct,
        "display_name": display_name or None,
        "profile_image_src": profile_image_src,
        "is_official_account": is_official_account,
        "is_certified_employer": is_certified_employer,
        "identity_verified": identity_verified,
    }
    return out


def _crowdworks_get_cached_profile_stats(uid: int, now_u: float) -> dict[str, Any] | None:
    tup = _CROWDWORKS_EMPLOYER_STATS_CACHE_V1.get(uid)
    if not tup:
        return None
    ttl = _crowdworks_profile_cache_ttl_s()
    if ttl <= 0:
        _CROWDWORKS_EMPLOYER_STATS_CACHE_V1.pop(uid, None)
        return None
    ts, cached = tup
    if (now_u - ts) > ttl:
        return None
    return cached


def _crowdworks_set_cached_profile_stats(uid: int, now_u: float, stats: dict[str, Any] | None) -> None:
    if stats is not None:
        _CROWDWORKS_EMPLOYER_STATS_CACHE_V1[uid] = (now_u, stats)


def fetch_crowdworks_employer_profile_stats_cached(
    session: requests.Session, employer_user_id: int, timeout: float
) -> dict[str, Any] | None:
    now_u = time.time()
    cached = _crowdworks_get_cached_profile_stats(employer_user_id, now_u)
    if cached is not None:
        return cached
    url = f"https://crowdworks.jp/public/employers/{employer_user_id}"
    try:
        page = fetch_html(session, url, timeout)
    except requests.RequestException:
        LOG.debug("CrowdWorks: employer profile fetch failed for user_id=%s", employer_user_id, exc_info=True)
        return None
    stats = parse_crowdworks_employer_profile_stats(page)
    if stats:
        _crowdworks_set_cached_profile_stats(employer_user_id, now_u, stats)
    return stats


def enrich_crowdworks_jobs_with_employer_profiles(session: requests.Session, jobs: list[JobListing], timeout: float) -> None:
    """
    Hydrate CrowdWorks client metrics from `/public/employers/{id}` when the listing JSON lacks them.

    Controlled by ``CROWDWORKS_FETCH_EMPLOYER_PROFILE`` (default: on). One GET per distinct employer ID per TTL window.
    """
    if not env_bool("CROWDWORKS_FETCH_EMPLOYER_PROFILE", True):
        return
    if not jobs:
        return
    need: dict[int, None] = {}
    for job in jobs:
        if not job.detail_url.strip() or not job_detail_url_is_crowdworks(job.detail_url):
            continue
        uid = crowdworks_extract_employer_user_id(job.client_profile_url)
        if uid is None:
            continue
        need[uid] = None
    if not need:
        return
    fetched: dict[int, dict[str, Any] | None] = {}
    for uid in need:
        st = fetch_crowdworks_employer_profile_stats_cached(session, uid, timeout)
        fetched[uid] = st if isinstance(st, dict) else None

    for job in jobs:
        if not job.detail_url.strip() or not job_detail_url_is_crowdworks(job.detail_url):
            continue
        uid = crowdworks_extract_employer_user_id(job.client_profile_url)
        if uid is None:
            continue
        s = fetched.get(uid)
        if not s:
            continue
        dn = s.get("display_name")
        if isinstance(dn, str) and dn.strip():
            job.client_name = dn.strip()
        imgs = s.get("profile_image_src")
        au = absolutize_crowdworks_asset(imgs) if isinstance(imgs, str) and imgs.strip() else None
        if au and not (job.client_avatar_url or "").strip():
            job.client_avatar_url = au
        avg = s.get("average_rating")
        if isinstance(avg, bool):
            avg = None
        if isinstance(avg, (int, float)) and math.isfinite(float(avg)):
            job.client_rating = float(max(0.0, min(5.0, float(avg))))
        # 「完了」= ``total_acceptance_count``, 「契約」= ``total_finished_count`` (CrowdWorks JP UI).
        kanryo = s.get("total_acceptance_count")
        keiyaku = s.get("total_finished_count")
        if isinstance(keiyaku, int):
            job.client_orders = str(keiyaku)
        ach = s.get("job_offer_achievement_count")
        pct = s.get("completion_rate_pct")
        cw_bits: list[str] = []
        if isinstance(ach, int):
            cw_bits.append(f"募集実績 {ach}")
        if isinstance(kanryo, int):
            cw_bits.append(f"完了 {kanryo}")
        if isinstance(keiyaku, int):
            cw_bits.append(f"契約 {keiyaku}")
        if isinstance(pct, int) and 0 <= pct <= 100:
            cw_bits.append(f"完了率 {pct}%")
        if cw_bits:
            line = " · ".join(cw_bits)
            ex = job.client_extras.strip()
            if not ex:
                job.client_extras = line
            elif line not in ex:
                job.client_extras = f"{ex} · {line}" if ex else line

        # 事業主体（個人/法人）推定の補助シグナルを保持（ingest 経由で rawData に乗る）。
        official = s.get("is_official_account")
        if isinstance(official, bool):
            job.client_is_official_account = official
        certified = s.get("is_certified_employer")
        if isinstance(certified, bool):
            job.client_is_certified_employer = certified
        verified = s.get("identity_verified")
        if isinstance(verified, bool):
            job.client_identity_verified = verified


def job_detail_url_is_lancers(detail_url: str) -> bool:
    host = (urlparse(detail_url).hostname or "").lower()
    return host.endswith("lancers.jp")


def lancers_client_profile_canonical_path(client_profile_url: str | None) -> str | None:
    """Return ``/client/slug`` for Lancers client profile links; ``None`` for unrelated URLs."""
    if not client_profile_url or not isinstance(client_profile_url, str):
        return None
    s = client_profile_url.strip()
    if s.startswith("//"):
        path = urlparse(f"https:{s}").path or ""
    elif s.startswith("http://") or s.startswith("https://"):
        path = urlparse(s).path or ""
    else:
        path = s if s.startswith("/") else f"/{s}"
    mo = LANCERS_CLIENT_PROFILE_PATH_RE.search(path.replace("\\", "/"))
    return f"/client/{mo.group(1)}" if mo else None


def _lancers_client_profile_cache_ttl_s() -> float:
    raw = os.environ.get("LANCERS_CLIENT_PROFILE_CACHE_SECONDS", "3600").strip()
    try:
        n = float(raw)
        return max(60.0, n) if n > 0 else 0.0
    except ValueError:
        return 3600.0


def _lancers_strip_num_token(raw: str) -> str | None:
    """First integer-like token from a header summary cell (`19`, `1,866`)."""
    t = " ".join(raw.replace(",", "").split())
    mo = re.search(r"\d+", t)
    return mo.group(0) if mo else None


def _lancers_int_from_vis(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, str):
        t = val.strip().replace(",", "")
        try:
            n = int(t)
            return n if n >= 0 else None
        except ValueError:
            return None
    if isinstance(val, (int, float)):
        if not math.isfinite(float(val)):
            return None
        n = int(val)
        return n if n >= 0 else None
    return None


def parse_lancers_client_profile_stats(page_html: str) -> dict[str, Any] | None:
    """
    Parsed header summary table from ``/client/{nickname}``: 発注数, 評価(良・悪件数),
    発注率 breakdown, optional 業種 dl — mirrors what the JP profile shows above the carousel.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    h1 = soup.select_one(".p-profile-header h1") or soup.select_one(".p-profile-media__heading")
    display_name = " ".join(h1.get_text(separator=" ", strip=True).split()) if h1 else ""

    tbl = soup.select_one(".p-profile-header-summary.c-table-summary")
    if not tbl:
        base: dict[str, Any] = {"display_name": display_name.strip() or None}
        avatar_img = soup.select_one(".p-profile-header img.c-avatar__image")
        if avatar_img:
            base["avatar_src"] = avatar_img.get("src")
        dl = soup.select_one("dl.c-definition-list")
        if dl:
            dt = dl.select_one("dt")
            dd = dl.select_one("dd")
            if dt and dd:
                dt_txt = dt.get_text(" ", strip=True)
                if "業種" in dt_txt and ("発注" in dt_txt or "希望" in dt_txt):
                    ind = " ".join(dd.get_text(separator=" ", strip=True).split())
                    base["preferred_industry"] = ind or None
        if base.get("display_name") or base.get("avatar_src") or base.get("preferred_industry"):
            return base
        return None

    oc: int | None = None
    rate_pct: int | None = None
    rate_num: int | None = None
    rate_den: int | None = None
    fb_good: int | None = None
    fb_bad: int | None = None
    continue_cnt: int | None = None

    cols = tbl.select(":scope > .c-table-summary__col")
    for col in cols:
        head = col.select_one(".c-table-summary__col-head")
        if not head:
            continue
        label_raw = head.get_text(" ", strip=True)
        label = " ".join(label_raw.split())
        if "発注数" in label and "発注率" not in label:
            num_el = col.select_one(".p-profile-header-summary__description-text span")
            if num_el:
                oc = _lancers_int_from_vis(num_el.get_text(strip=True))
            else:
                ntxt = col.select_one(".p-profile-header-summary__description-text")
                if ntxt:
                    tk = _lancers_strip_num_token(ntxt.get_text(" ", strip=True))
                    oc = _lancers_int_from_vis(tk) if tk else None

        elif "評価" in label and "発注" not in label:
            nums: list[int] = []
            for item in col.select(".p-profile-header-summary__description-item .p-profile-header-summary__description-text"):
                for sp in item.select("span"):
                    if "u-unit" in (sp.get("class") or []):
                        continue
                    n = _lancers_int_from_vis(sp.get_text(strip=True))
                    if n is not None:
                        nums.append(n)
            if len(nums) >= 1:
                fb_good = nums[0]
            if len(nums) >= 2:
                fb_bad = nums[1]

        elif "発注率" in label:
            pct_el = col.select_one(".p-profile-header-summary__description-text span")
            if pct_el:
                pct_txt = pct_el.get_text(strip=True).replace(",", "")
                if pct_txt.strip() != "---":
                    rate_pct = _lancers_int_from_vis(pct_txt)
            note = col.select_one(".c-table-summary__col-note")
            if note:
                note_t = note.get_text(" ", strip=True)
                mo_br = re.search(r"(\d+)\s*[/／]\s*([\d,\s]+)", note_t)
                if mo_br:
                    rate_num = _lancers_int_from_vis(mo_br.group(1))
                    rate_den = _lancers_int_from_vis(mo_br.group(2).replace(",", "").replace(" ", ""))

        elif "継続ランサー" in label:
            num_el = col.select_one(".p-profile-header-summary__description-text span")
            if num_el:
                tx = num_el.get_text(strip=True).replace(",", "")
                if tx != "---":
                    continue_cnt = _lancers_int_from_vis(tx)

    out: dict[str, Any] = {
        "display_name": display_name.strip() or None,
        "order_count": oc,
        "order_rate_pct": rate_pct,
        "order_rate_num": rate_num,
        "order_rate_den": rate_den,
        "feedback_good": fb_good,
        "feedback_bad": fb_bad,
        "continuing_lancers": continue_cnt,
    }
    avatar_img = soup.select_one(".p-profile-header img.c-avatar__image")
    if avatar_img:
        out["avatar_src"] = avatar_img.get("src")
    dl = soup.select_one("dl.c-definition-list")
    if dl:
        dt = dl.select_one("dt")
        dd = dl.select_one("dd")
        if dt and dd:
            dt_txt = dt.get_text(" ", strip=True)
            if "業種" in dt_txt and ("発注" in dt_txt or "希望" in dt_txt):
                ind = " ".join(dd.get_text(separator=" ", strip=True).split())
                out["preferred_industry"] = ind or None

    has_metric = any(
        out.get(k) is not None
        for k in (
            "order_count",
            "order_rate_pct",
            "order_rate_num",
            "order_rate_den",
            "feedback_good",
            "feedback_bad",
            "continuing_lancers",
        )
    )
    name_ok = bool((out.get("display_name") or "").strip())
    if name_ok or has_metric or bool(out.get("avatar_src")) or bool(out.get("preferred_industry")):
        return out
    return None


def _lancers_get_cached_client_profile(path_key: str, now_u: float) -> dict[str, Any] | None:
    tup = _LANCERS_CLIENT_STATS_CACHE_V1.get(path_key)
    if not tup:
        return None
    ttl = _lancers_client_profile_cache_ttl_s()
    if ttl <= 0:
        _LANCERS_CLIENT_STATS_CACHE_V1.pop(path_key, None)
        return None
    ts, cached = tup
    if (now_u - ts) > ttl:
        return None
    return cached


def _lancers_set_cached_client_profile(path_key: str, now_u: float, stats: dict[str, Any] | None) -> None:
    if stats is not None:
        _LANCERS_CLIENT_STATS_CACHE_V1[path_key] = (now_u, stats)


def fetch_lancers_client_profile_stats_cached(session: requests.Session, path_key: str, timeout: float) -> dict[str, Any] | None:
    """path_key canonical ``/client/slug`` (case preserved)."""
    now_u = time.time()
    cached = _lancers_get_cached_client_profile(path_key, now_u)
    if cached is not None:
        return cached
    url = urljoin("https://www.lancers.jp/", path_key.lstrip("/"))
    try:
        page = fetch_html(session, url, timeout)
    except requests.RequestException:
        LOG.debug("Lancers: client profile fetch failed for %s", path_key, exc_info=True)
        return None
    stats = parse_lancers_client_profile_stats(page)
    if stats:
        _lancers_set_cached_client_profile(path_key, now_u, stats)
    return stats


def _append_lancers_client_extras(job: JobListing, parts: list[str]) -> None:
    if not parts:
        return
    line = " · ".join(p for p in parts if p)
    if not line:
        return
    ex = job.client_extras.strip()
    if not ex:
        job.client_extras = line
    elif line not in ex:
        job.client_extras = f"{ex} · {line}" if ex else line


def enrich_lancers_jobs_with_client_profiles(session: requests.Session, jobs: list[JobListing], timeout: float) -> None:
    """
    Hydrate sparse listing cards via ``/client/{nickname}`` profile header metrics.

    Default on (``LANCERS_FETCH_CLIENT_PROFILE``). One GET per distinct client path within the TTL cache.
    """
    if not env_bool("LANCERS_FETCH_CLIENT_PROFILE", True):
        return
    if not jobs:
        return
    paths: dict[str, None] = {}
    for job in jobs:
        if not job.detail_url.strip() or not job_detail_url_is_lancers(job.detail_url):
            continue
        pth = lancers_client_profile_canonical_path(job.client_profile_url)
        if not pth:
            continue
        paths[pth] = None

    fetched: dict[str, dict[str, Any] | None] = {}
    for pkey in paths:
        st = fetch_lancers_client_profile_stats_cached(session, pkey, timeout)
        fetched[pkey] = st if isinstance(st, dict) else None

    for job in jobs:
        if not job.detail_url.strip() or not job_detail_url_is_lancers(job.detail_url):
            continue
        pkey = lancers_client_profile_canonical_path(job.client_profile_url)
        if not pkey:
            continue
        stats = fetched.get(pkey)
        if not stats:
            continue

        dn = stats.get("display_name")
        if isinstance(dn, str) and dn.strip():
            job.client_name = dn.strip()

        oc = stats.get("order_count")
        if isinstance(oc, int):
            job.client_orders = str(oc)

        av = stats.get("avatar_src")
        avu = absolutize_lancers_asset(av) if isinstance(av, str) and av.strip() else None
        if avu and not (job.client_avatar_url or "").strip():
            job.client_avatar_url = avu

        extras_parts: list[str] = []

        pct = stats.get("order_rate_pct")
        on = stats.get("order_rate_num")
        od = stats.get("order_rate_den")
        if isinstance(pct, int) and 0 <= pct <= 100:
            if isinstance(on, int) and isinstance(od, int) and od > 0:
                extras_parts.append(f"発注率 {pct}%（{on}/{od}）")
            else:
                extras_parts.append(f"発注率 {pct}%")

        gf = stats.get("feedback_good")
        bf = stats.get("feedback_bad")
        if isinstance(gf, int) and isinstance(bf, int):
            extras_parts.append(f"フィードバック 良{gf}・悪{bf}")
        elif isinstance(gf, int):
            extras_parts.append(f"フィードバック（良）{gf}件")

        clancers = stats.get("continuing_lancers")
        if isinstance(clancers, int) and clancers > 0:
            extras_parts.append(f"継続ランサー {clancers}人")

        _append_lancers_client_extras(job, extras_parts)


def parse_crowdworks_listings(page_html: str, listing_url: str) -> list[JobListing]:
    """Parse embedded ``#vue-container`` JSON from CrowdWorks search (first result page)."""
    soup = BeautifulSoup(page_html, "html.parser")
    vc = soup.select_one("#vue-container")
    if not vc:
        LOG.warning(
            "CrowdWorks: no #vue-container in %s — page structure may have changed.",
            listing_url[:100],
        )
        return []
    raw_data = vc.get("data") or ""
    try:
        payload = json.loads(html.unescape(raw_data))
    except json.JSONDecodeError as e:
        LOG.warning("CrowdWorks: could not parse vue-container JSON (%s): %s", listing_url[:80], e)
        return []

    offers = (payload.get("searchResult") or {}).get("job_offers") or []
    out: list[JobListing] = []
    for row in offers:
        if not isinstance(row, dict):
            continue
        jo = row.get("job_offer") or {}
        if not isinstance(jo, dict):
            jo = {}
        jid = jo.get("id")
        if jid is None:
            continue
        try:
            wid = str(int(jid))
        except (TypeError, ValueError):
            continue

        title = " ".join((jo.get("title") or "").split())
        summary = " ".join((jo.get("description_digest") or "").split())
        client = row.get("client") or {}
        if not isinstance(client, dict):
            client = {}

        uname = _cw_client_display_name(client, row)
        uid = _cw_client_uid(client, row)
        avatar = absolutize_crowdworks_asset((client.get("user_picture_url") or "").strip() or None)
        profile_href: str | None
        try:
            profile_href = f"/public/employers/{int(uid)}" if uid is not None else None
        except (TypeError, ValueError):
            profile_href = None

        project_entry = _cw_project_entry(row)
        orders = _cw_orders_text(row, project_entry, client)
        cw_rating = _cw_rating_from_payload(client, row, jo)
        completion = _cw_completion_rate_pct(row, client)
        extras_parts: list[str] = []
        if completion is not None:
            extras_parts.append(f"完了率 {completion}%")
        client_extras = " · ".join(extras_parts) if extras_parts else ""

        payment = row.get("payment")
        budget = crowdworks_payment_summary(payment if isinstance(payment, dict) else None)

        detail = f"https://crowdworks.jp/public/jobs/{wid}"
        out.append(
            JobListing(
                work_id=wid,
                title=title,
                budget_text=budget,
                detail_url=detail,
                client_name=uname,
                client_profile_url=profile_href,
                client_extras=client_extras,
                source_listing_url=listing_url,
                listing_summary=summary,
                client_avatar_url=avatar,
                client_orders=orders,
                client_rating=cw_rating,
            )
        )
    return out


def _listing_card_title_anchor(card: Tag) -> Tag | None:
    """Title link inside a listing card — main grid vs 「新着」carousel markup differ."""
    title_a = card.select_one("a.p-search-job-media__title")
    if title_a:
        return title_a
    title_a = card.select_one("a.p-search-job__latest-media-title")
    if title_a:
        return title_a
    return card.select_one("a[href^='/work/detail/']")


def parse_listings(page_html: str, listing_url: str) -> list[JobListing]:
    if listing_url_is_crowdworks(listing_url):
        return parse_crowdworks_listings(page_html, listing_url)
    soup = BeautifulSoup(page_html, "html.parser")
    out: list[JobListing] = []
    # 新着カルーセル行には発注者・/client/ リンクが無い。取り込むと client が空のまま DB / Discord に載るため除外する。
    main_cards = soup.select("div.p-search-job-media.c-media")
    cards = main_cards
    seen_on_page: set[str] = set()
    for card in cards:
        title_a = _listing_card_title_anchor(card)
        if not title_a:
            continue
        tags_ul = title_a.select_one("ul.p-search-job-media__tags")
        if tags_ul:
            tags_ul.extract()
        href = title_a.get("href") or ""
        m = WORK_ID_RE.search(href)
        if not m:
            continue
        wid = m.group(1)
        if wid in seen_on_page:
            continue
        seen_on_page.add(wid)
        title_text = " ".join(title_a.get_text(separator=" ", strip=True).split())
        budget = parse_budget(card)
        cname, chref, extras, cavatar, corders, crating = parse_client(card)
        fb_orders, fb_rating = _lancers_metrics_regex_fallback(card)
        if corders is None and fb_orders is not None:
            corders = fb_orders
        if crating is None and fb_rating is not None:
            crating = fb_rating
        detail = urljoin("https://www.lancers.jp/", href.lstrip("/"))
        listing_summary = parse_listing_summary_text(card)
        out.append(
            JobListing(
                work_id=wid,
                title=title_text,
                budget_text=budget,
                detail_url=detail,
                client_name=cname,
                client_profile_url=chref,
                client_extras=extras,
                source_listing_url=listing_url,
                listing_summary=listing_summary,
                client_avatar_url=cavatar,
                client_orders=corders,
                client_rating=crating,
            )
        )
    return out


def fetch_live_listings_snapshot(session: requests.Session, urls: list[str], timeout: float) -> list[JobListing]:
    """Same parsing path as polling, flattened for browser preview."""
    merged: list[JobListing] = []
    keyed: set[tuple[str, str]] = set()
    for listing_url in urls:
        blob = fetch_html(session, listing_url, timeout)
        for job in parse_listings(blob, listing_url):
            k = (job.work_id, job.source_listing_url)
            if k in keyed:
                continue
            keyed.add(k)
            merged.append(job)
    enrich_crowdworks_jobs_with_employer_profiles(session, merged, timeout)
    enrich_lancers_jobs_with_client_profiles(session, merged, timeout)
    return merged


def job_public_dict(job: JobListing) -> dict:
    """Serializable snapshot of extracted listing fields."""
    return asdict(job)


def plaintext_block_for_job(job: JobListing, request_body: str) -> str:
    """Single job as plain UTF-8 text for the clipboard (依頼詳細相当)."""
    client = job.client_summary()
    lines = [
        job.title,
        "",
        job.detail_url,
        "",
        f"予算（一覧）: {job.budget_text}",
        f"クライアント情報: {client.replace(chr(10), ' / ')}",
        f"案件ID: {job.work_id}",
        "",
        "【依頼詳細・依頼概要】",
        request_body.strip() if request_body.strip() else "(本文を取得できませんでした。「一覧」の要約のみ下記参照)",
        "",
        "【一覧に表示された要約】",
        job.listing_summary.strip() if job.listing_summary.strip() else "(なし)",
    ]
    return "\n".join(lines)


def _clipboard_plain_inner(session: requests.Session, jobs: list[JobListing], timeout: float, *, fetch_detail: bool) -> str:
    parts: list[str] = []
    sep = os.environ.get("CLIPBOARD_JOB_SEPARATOR", "\n\n" + "─" * 48 + "\n\n")

    for job in jobs:
        body = ""
        if fetch_detail:
            try:
                detail_html_text = fetch_html(session, job.detail_url, timeout)
                body = extract_job_detail_for_clipboard(detail_html_text, job.detail_url)
            except requests.RequestException as e:
                LOG.warning("Clipboard: could not fetch work detail %s (%s)", job.work_id, e)
        if not body.strip():
            body = job.listing_summary
        parts.append(plaintext_block_for_job(job, body))

    return sep.join(parts)


def clipboard_text_from_jobs(
    session: requests.Session,
    jobs: list[JobListing],
    timeout: float,
    *,
    fetch_detail: bool,
    urls_priority: list[str] | None = None,
) -> str:
    """Clipboard payload; grouped by listing URL when multiple categories appear."""
    if not jobs:
        return ""
    by_src: defaultdict[str, list[JobListing]] = defaultdict(list)
    for j in jobs:
        by_src[j.source_listing_url].append(j)

    order = urls_priority if urls_priority else sorted(by_src.keys())
    sep_outer = os.environ.get("CLIPBOARD_SECTION_SEPARATOR", "\n\n" + "=" * 52 + "\n\n")

    sections: list[str] = []
    seen_sources: set[str] = set()
    for src in order:
        bucket = by_src.get(src)
        if not bucket:
            continue
        seen_sources.add(src)
        label = listing_category_display_label(src)
        inner = _clipboard_plain_inner(session, bucket, timeout, fetch_detail=fetch_detail)
        sections.append(f"【カテゴリ: {label}】\n{src}\n\n{inner}")

    for src in sorted(by_src.keys()):
        if src in seen_sources:
            continue
        bucket = by_src[src]
        label = listing_category_display_label(src)
        inner = _clipboard_plain_inner(session, bucket, timeout, fetch_detail=fetch_detail)
        sections.append(f"【カテゴリ: {label}】\n{src}\n\n{inner}")

    return sep_outer.join(sections)


def copy_plain_text_best_effort(text: str) -> bool:
    if not text.strip():
        return False

    try:
        import pyperclip  # type: ignore[import-untyped]

        pyperclip.copy(text)
        return True
    except Exception:
        pass

    data = text.encode("utf-8")
    exe_and_args: list[tuple[list[str], bytes]] = []
    if sys.platform.startswith("linux"):
        wl = shutil.which("wl-copy")
        if wl:
            exe_and_args.append(([wl], data))
        xc = shutil.which("xclip")
        if xc:
            exe_and_args.append([[xc, "-selection", "clipboard"], data])
        xs = shutil.which("xsel")
        if xs:
            exe_and_args.append([[xs, "--clipboard", "--input"], data])
    elif sys.platform == "darwin":
        pb = shutil.which("pbcopy")
        if pb:
            exe_and_args.append([[pb], data])

    for argv, stdin in exe_and_args:
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, timeout=15, check=False)
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue

    return False


def split_discord_chunks(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit
    return chunks


def format_rating_with_stars(rating: float, *, scale: float = 5.0) -> str:
    """Five-slot star row (★ / ☆) plus numeric score for Discord."""
    r = max(0.0, min(float(rating), scale))
    slots = 5
    filled = int(round((r / scale) * slots)) if scale > 0 else 0
    filled = max(0, min(slots, filled))
    stars = "★" * filled + "☆" * (slots - filled)
    return f"{stars} **`{r:g}`** / `{scale:g}`"


def format_rating_plain_text(rating: float, *, scale: float = 5.0) -> str:
    """Stars + numeric score without Discord markdown (for HTML previews)."""
    r = max(0.0, min(float(rating), scale))
    slots = 5
    filled = int(round((r / scale) * slots)) if scale > 0 else 0
    filled = max(0, min(slots, filled))
    stars = "★" * filled + "☆" * (slots - filled)
    return f"{stars} {r:g} / {scale:g}"


def _discord_platform_branding(detail_url: str) -> tuple[str, int, dict[str, str], dict[str, str]]:
    """Intro line, embed color (RGB int), author block, footer — Lancers vs クラウドワークス."""
    host = (urlparse(detail_url).hostname or "").lower()
    if host.endswith("crowdworks.jp"):
        intro = "**【クラウドワークス】** 新しく検出された案件です。"
        color = 0xEA580E
        author = {
            "name": "クラウドワークス · CrowdWorks",
            "url": "https://crowdworks.jp/",
            "icon_url": "https://crowdworks.jp/favicon.ico",
        }
        footer = {
            "text": "CrowdWorks.jp 通知",
            "icon_url": "https://crowdworks.jp/favicon.ico",
        }
        return intro, color, author, footer

    intro = "**【Lancers.jp】** 求人ボードで新しく検出された案件です。"
    color = 0x2563EB
    author = {
        "name": "Lancers",
        "url": "https://www.lancers.jp/",
        "icon_url": "https://www.lancers.jp/favicon.ico",
    }
    footer = {
        "text": "Lancers.jp 通知",
        "icon_url": "https://www.lancers.jp/favicon.ico",
    }
    return intro, color, author, footer


def job_to_discord_embed(job: JobListing) -> dict:
    """One Discord embed for a listing (combined up to 10 per webhook POST)."""
    title = job.title[:250] + ("…" if len(job.title) > 250 else "")
    client_display = job.client_name.strip() or "クライアント"

    du = urlparse(job.detail_url)
    intro, embed_color, author_block, footer_block = _discord_platform_branding(job.detail_url)
    is_cw = job_detail_url_is_crowdworks(job.detail_url)
    desc_lines = [
        intro,
        "",
        f"**👤 {client_display}**",
    ]
    if job.client_profile_url:
        ch = job.client_profile_url.strip()
        if ch.startswith("http://") or ch.startswith("https://"):
            pf = ch
        else:
            base = f"{du.scheme}://{du.netloc}" if du.scheme and du.netloc else "https://www.lancers.jp"
            pf = urljoin(base + "/", ch.lstrip("/"))
        desc_lines.append(f"[プロフィールを開く]({pf})")

    orders_label = "契約数" if is_cw else "発注数"
    if job.client_orders:
        desc_lines.append(f"📦 **{orders_label}:** `{job.client_orders}`")
    else:
        desc_lines.append(f"📦 **{orders_label}:** —")

    rating_heading = "総合評価" if is_cw else "評価"
    if job.client_rating is not None:
        desc_lines.append(f"⭐ **{rating_heading}:** {format_rating_with_stars(job.client_rating)}")
    else:
        desc_lines.append(f"⭐ **{rating_heading}:** —")

    ex = job.client_extras.strip()
    if ex:
        desc_lines.append("")
        desc_lines.append(f"📊 {ex}")

    description = "\n".join(desc_lines)
    if len(description) > 4096:
        description = description[:4093] + "…"

    embed: dict = {
        "title": title,
        "url": job.detail_url,
        "description": description,
        "color": embed_color,
        "author": author_block,
        "footer": footer_block,
        "fields": [
            {"name": "💰 予算（一覧表示）", "value": job.budget_text[:1024]},
            {"name": "📎 求人URL", "value": job.detail_url[:1024]},
            {"name": "📂 監視ソース", "value": job.source_listing_url[:1024]},
            {"name": "案件 ID", "value": job.work_id[:1024]},
        ],
    }

    au = job.client_avatar_url
    if au:
        embed["thumbnail"] = {"url": au[:2048]}

    return embed


def build_live_preview_html(jobs: list[JobListing]) -> bytes:
    """Single HTML page summarizing extracted JobListing rows."""
    rows: list[str] = []
    for j in jobs:
        if j.client_avatar_url:
            au = html.escape(j.client_avatar_url, quote=True)
            avatar = (
                f'<img src="{au}" width="44" height="44" alt="" loading="lazy" '
                'referrerpolicy="no-referrer" style="object-fit:cover;border-radius:6px;display:block;">'
            )
        else:
            avatar = "—"

        rat_line = (
            format_rating_plain_text(j.client_rating) if j.client_rating is not None else "評価: —"
        )
        sub_client = html.escape(j.client_orders or "—", quote=False) + " · " + html.escape(rat_line, quote=False)

        snippet = j.listing_summary.strip()
        if len(snippet) > 220:
            snippet = snippet[:220] + "…"
        snippet_esc = html.escape(snippet or "—", quote=False)

        du = html.escape(j.detail_url, quote=True)

        rows.append(
            "<tr>"
            f"<td>{avatar}</td>"
            f"<td><span class='cat'>{html.escape(listing_category_label(j.source_listing_url), quote=False)}</span><br>"
            f"<span class='mono'><a href='{du}'>{html.escape(j.work_id, quote=False)}</a></span></td>"
            f"<td>{html.escape(j.title, quote=False)}</td>"
            f"<td class='mono'>{html.escape(j.budget_text, quote=False)}</td>"
            f"<td>{html.escape(j.client_name or '—', quote=False)}<br><span class='sub'>{sub_client}</span></td>"
            f"<td class='snip'>{snippet_esc}</td>"
            f"<td class='mono'><a href='{du}'>detail</a></td>"
            "</tr>"
        )

    body = "\n".join(rows)
    page = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Job boards · structured preview</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#f1f5f9;color:#0f172a;}}
.note{{opacity:.85;font-size:.9rem;max-width:900px;line-height:1.5;margin-bottom:14px;}}
table{{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 4px rgba(15,23,42,.06);border-radius:6px;overflow:hidden;}}
th,td{{border:1px solid #e2e8f0;padding:10px;vertical-align:top;font-size:.86rem;line-height:1.35;}}
th{{background:#e8f1ff;text-align:left;font-weight:600;}}
.mono{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.82rem;}}
.cat{{color:#2563eb;font-weight:700;font-size:.8rem;}}
.sub{{font-size:.8rem;opacity:.88;display:block;margin-top:4px;line-height:1.4;}}
.snip{{max-width:360px;color:#475569;font-size:.8rem;}}</style></head><body>
<h1 style="margin:0 0 8px;font-size:1.2rem;">Listing snapshot (Lancers · CrowdWorks)</h1>
<p class="note">Fields match what <code>monitor.py</code> parses from category search HTML ({len(jobs)} row(s)).
<a href="/json">Raw JSON array</a> · stop the process with Ctrl+C.</p>
<table><thead><tr>
<th scope="col" style="width:56px;"></th><th scope="col" style="width:120px;">カテゴリ / ID</th>
<th scope="col">タイトル</th><th scope="col" style="width:140px;">予算</th>
<th scope="col">クライアント<br><small>発注・評価</small></th><th scope="col">一覧要約（抜粋）</th>
<th scope="col" style="width:72px;">リンク</th></tr></thead><tbody>{body}</tbody></table></body></html>
"""
    return page.encode("utf-8")


def serve_structured_preview(bind: str, port: int, jobs: list[JobListing]) -> None:
    json_bytes = json.dumps([job_public_dict(j) for j in jobs], ensure_ascii=False, indent=2).encode("utf-8")
    html_bytes = build_live_preview_html(jobs)

    class StructuredPreviewHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args_: object) -> None:
            LOG.info("preview %s", fmt % args_)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(json_bytes)))
                self.end_headers()
                self.wfile.write(json_bytes)
                return
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return
            self.send_error(404, "Use / or /json")

    httpd = HTTPServer((bind, port), StructuredPreviewHandler)
    httpd.serve_forever()


def discord_retry_after_seconds(response: requests.Response) -> float:
    hdr = response.headers.get("Retry-After")
    if hdr is not None and str(hdr).strip() != "":
        try:
            return max(0.0, float(str(hdr).strip()))
        except ValueError:
            pass
    try:
        rf = response.json().get("retry_after")
        if isinstance(rf, (int, float)):
            return max(0.0, float(rf))
        if isinstance(rf, str) and rf.strip():
            return max(0.0, float(rf.strip()))
    except Exception:
        pass
    return 2.0


def discord_execute_webhook(webhook_url: str, payload: dict) -> None:
    """POST to Discord webhook; backoff and retry on HTTP 429."""
    margin = float(os.environ.get("DISCORD_RATE_LIMIT_MARGIN_SECONDS", "0.15"))
    retries_max = int(os.environ.get("DISCORD_WEBHOOK_MAX_RETRIES", "24"))
    n_embeds = len(payload.get("embeds") or ())
    if n_embeds:
        phase = "%d embed(s)" % n_embeds
    elif isinstance(payload.get("content"), str) and payload["content"]:
        phase = "text notice"
    else:
        phase = "webhook"

    consecutive_429 = 0
    while True:
        r = requests.post(
            webhook_url,
            json=payload,
            timeout=30,
            headers={"User-Agent": "JobHunter-Webhook/1.0"},
        )

        if r.status_code == 429:
            if consecutive_429 >= retries_max:
                raise RuntimeError(
                    "Discord webhook HTTP 429 after %d backoff retries — %s"
                    % (
                        retries_max,
                        r.text[:400] if r.text else "",
                    )
                )
            consecutive_429 += 1
            delay = discord_retry_after_seconds(r) + margin
            LOG.warning(
                "Discord HTTP 429 (%s); sleeping %.2fs then retry %d/%d",
                phase,
                delay,
                consecutive_429,
                retries_max,
            )
            time.sleep(delay)
            continue

        if r.status_code == 405:
            raise RuntimeError(
                "Discord returned HTTP 405 — the webhook URL is usually wrong or incomplete "
                "(missing token after the last /). Regenerate the webhook in Discord and paste the full "
                "'Copy Webhook URL' into DISCORD_WEBHOOK_URL."
            )
        if r.status_code not in (200, 204):
            raise RuntimeError(f"Discord webhook failed: HTTP {r.status_code} {r.text[:500]}")
        return


def discord_notify_monitor_started(
    webhook_url: str,
    *,
    once_mode: bool,
    interval_seconds: int,
    listing_urls_for_this_channel: list[str],
) -> None:
    """One short webhook message confirming the watcher has started."""
    try:
        host = socket.gethostname()
    except OSError:
        host = "(不明)"
    mode = "**--once** (1回だけポーリングして終了)" if once_mode else f"連続運転（約 **{interval_seconds}** 秒ごと）"
    cat_lines = "\n".join(
        f"- **{listing_category_display_label(u)}**: `{u}`" for u in sorted(listing_urls_for_this_channel)
    )
    content = (
        "**Job board monitor 起動しました**\n"
        "求人ボード監視プロセスが立ち上がりました。\n\n"
        f"- ホスト: `{host}`\n"
        f"- PID: `{os.getpid()}`\n"
        f"- モード: {mode}\n"
        f"- このチャンネルへのカテゴリ:\n{cat_lines}"
    ).strip()

    discord_execute_webhook(webhook_url, {"content": content[:2000]})


def post_discord_new_jobs(webhook_url: str, jobs: list[JobListing]) -> None:
    """Send Discord notifications batched as multiple embeds per request (fewer POSTs → fewer 429s)."""
    if not jobs:
        return

    raw_lim = os.environ.get("DISCORD_MAX_EMBEDS_PER_MESSAGE", "10").strip()
    chunk_size = 10
    if raw_lim:
        try:
            chunk_size = max(1, min(10, int(raw_lim)))
        except ValueError:
            chunk_size = 10

    pause = float(os.environ.get("DISCORD_POST_DELAY_SECONDS", "1.25"))

    total = len(jobs)
    for offset in range(0, total, chunk_size):
        chunk = jobs[offset : offset + chunk_size]
        payload = {"embeds": [job_to_discord_embed(j) for j in chunk]}
        discord_execute_webhook(webhook_url, payload)
        end = offset + len(chunk)
        if end < total:
            LOG.debug("Discord batch pause %.2fs before next webhook (%d/%d jobs)", pause, end, total)
            time.sleep(pause)


def load_state(path: Path, urls: list[str]) -> dict[str, set[str]]:
    buckets = monitor_category_buckets(urls)
    empty: dict[str, set[str]] = {k: set() for k in buckets}

    if not path.is_file():
        return empty
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        LOG.warning("Could not read state %s (%s); starting fresh", path, e)
        return empty
    if not raw:
        LOG.info("State file %s is empty — will seed on first poll if needed", path)
        return empty
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        LOG.warning("Invalid JSON in state %s (%s); starting fresh", path, e)
        return empty

    known: dict[str, set[str]] = {k: set() for k in buckets}

    by_cat = data.get("known_ids_by_category")
    if isinstance(by_cat, dict):
        for key, ids in by_cat.items():
            if isinstance(key, str) and isinstance(ids, list):
                known.setdefault(key, set()).update(str(x) for x in ids)
        for k in buckets:
            known.setdefault(k, set())
        return known

    legacy_ids = data.get("known_ids")
    if isinstance(legacy_ids, list):
        merged = {str(x) for x in legacy_ids}
        for k in buckets:
            known[k].update(merged)
        LOG.info(
            "Migrated legacy flat known_ids (%d ids) into each category bucket: %s.",
            len(merged),
            ", ".join(buckets),
        )
        return known

    LOG.warning("State file missing known_ids_by_category / known_ids; starting fresh.")
    return empty


def save_state(path: Path, known: dict[str, set[str]], urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bucket_keys = monitor_category_buckets(urls)
    keys_to_save = sorted(set(bucket_keys) | set(known.keys()))
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "known_ids_by_category": {
            k: sorted(known.get(k, set()), key=lambda x: int(x), reverse=True) for k in keys_to_save
        },
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmppath = tempfile.mkstemp(prefix=".lancers_seen.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmppath, path)
    except Exception:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
        raise


def run_poll(
    session: requests.Session,
    listing_webhooks: dict[str, str],
    urls: list[str],
    state_path: Path,
    timeout: float,
    *,
    notify_on_first_poll: bool,
    already_known: dict[str, set[str]],
) -> dict[str, set[str]]:
    """
    Fetch all URLs, return updated known job IDs grouped by listing category (system / web / …).

    When notify_on_first_poll is False and every category bucket is empty, only seeds state.
    """
    poll_started_iso = _utc_iso()
    all_from_round: dict[str, JobListing] = {}
    ordered_ids: list[str] = []
    jobs_found_per_url: defaultdict[str, int] = defaultdict(int)
    listing_errors: dict[str, str | None] = {}

    for url in urls:
        try:
            html = fetch_html(session, url, timeout)
        except requests.RequestException as e:
            listing_errors[url] = str(e)[:500]
            LOG.error("Listing fetch failed %s: %s", url[:80], e)
            continue
        for job in parse_listings(html, url):
            jobs_found_per_url[url] += 1
            if job.work_id not in all_from_round:
                ordered_ids.append(job.work_id)
                all_from_round[job.work_id] = job

    enrich_crowdworks_jobs_with_employer_profiles(session, list(all_from_round.values()), timeout)
    enrich_lancers_jobs_with_client_profiles(session, list(all_from_round.values()), timeout)

    new_entries = []
    buckets = monitor_category_buckets(urls)
    known: dict[str, set[str]] = defaultdict(set)
    for k in buckets:
        known[k].update(already_known.get(k, set()))

    discord_by_detail_url: dict[str, tuple[str, bool, str | None]] = {}
    empty_state = sum(len(v) for v in known.values()) == 0
    suppress_all = empty_state and not notify_on_first_poll

    if suppress_all:
        for wid in ordered_ids:
            job = all_from_round[wid]
            cat = listing_category_label(job.source_listing_url)
            known.setdefault(cat, set()).add(wid)
        save_state(state_path, dict(known), urls)
        LOG.info(
            "First run / empty state — seeded category buckets (%s) without notifications",
            ", ".join(f"{k}:{len(known[k])}" for k in sorted(known.keys())),
        )
        return dict(known)

    for wid in ordered_ids:
        job = all_from_round[wid]
        cat = listing_category_label(job.source_listing_url)
        if wid not in known.setdefault(cat, set()):
            new_entries.append(job)

    # Oldest-unknown first within this batch so chronological order resembles listing order top-to-bottom
    for job in new_entries:
        LOG.info("New job detected: %s — %s", job.work_id, job.title[:80])
    if not new_entries:
        LOG.info(
            "Poll finished: no jobs to notify — every ID on this round's listing pages is already "
            "in `%s` (%d job card(s) scanned). Startup Discord messages are separate from job alerts.",
            state_path.name,
            len(all_from_round),
        )
    if new_entries:
        if env_bool("COPY_TO_CLIPBOARD", True):
            fetch_detail_cb = env_bool("CLIPBOARD_FETCH_WORK_DETAIL", True)
            blob = clipboard_text_from_jobs(
                session,
                new_entries,
                timeout,
                fetch_detail=fetch_detail_cb,
                urls_priority=urls,
            )
            if copy_plain_text_best_effort(blob):
                LOG.info("Copied request-detail text for %d new job(s) to the clipboard.", len(new_entries))
            else:
                LOG.warning(
                    "COPY_TO_CLIPBOARD is on but clipboard write failed "
                    "(headless SSH / installs: pip install pyperclip plus xclip, wl-copy, or desktop session)."
                )

        hooks_seen: list[str] = []
        hooks_added = set()
        for listing_u in urls:
            hook_u = listing_webhooks.get(listing_u)
            if hook_u is None:
                continue
            if hook_u not in hooks_added:
                hooks_added.add(hook_u)
                hooks_seen.append(hook_u)

        for hook_url in hooks_seen:
            bucket = [j for j in new_entries if listing_webhooks[j.source_listing_url] == hook_url]
            if bucket:
                try:
                    post_discord_new_jobs(hook_url, bucket)
                    for job in bucket:
                        discord_by_detail_url[job.detail_url] = (hook_url, True, None)
                except Exception as e:
                    err_msg = str(e)[:500]
                    LOG.error("Discord webhook delivery failed (%s jobs): %s", len(bucket), e)
                    for job in bucket:
                        discord_by_detail_url[job.detail_url] = (hook_url, False, err_msg)

    for wid in ordered_ids:
        job = all_from_round[wid]
        cat = listing_category_label(job.source_listing_url)
        known.setdefault(cat, set()).add(wid)

    poll_finished_iso = _utc_iso()
    try:
        push_dashboard_ingest(
            session,
            listing_webhooks,
            urls,
            dict(jobs_found_per_url),
            listing_errors,
            new_entries,
            discord_by_detail_url,
            poll_started_iso,
            poll_finished_iso,
        )
    except Exception as e:
        LOG.warning("Dashboard ingest unexpected error: %s", e)

    save_state(state_path, dict(known), urls)
    LOG.debug("Poll done; known counts by category: %s", {k: len(v) for k, v in sorted(known.items())})
    return dict(known)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Lancers.jp and CrowdWorks.jp listings; notify Discord.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Perform a single poll and exit.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override POLL_INTERVAL_SECONDS for looping mode.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete STATE_PATH and exit.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Fetch listing pages once and serve parsed data in the browser (HTTP); no Discord/state updates.",
    )
    parser.add_argument(
        "--preview-port",
        type=int,
        default=None,
        help="Port for --preview (defaults to PREVIEW_HTTP_PORT or 8790).",
    )
    parser.add_argument(
        "--preview-bind",
        type=str,
        default=None,
        help="Bind host for --preview (defaults to PREVIEW_HTTP_BIND or 127.0.0.1).",
    )
    args = parser.parse_args(argv)

    load_env_file_if_available()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )

    state_path_str = os.environ.get("STATE_PATH", "lancers_seen.json").strip()
    state_path = Path(state_path_str).expanduser().resolve()

    if args.reset_state:
        try:
            if state_path.is_file():
                state_path.unlink()
                LOG.info("Removed state file %s", state_path)
            else:
                LOG.info("No state file at %s", state_path)
        except OSError as e:
            LOG.error("Could not reset state: %s", e)
            return 1
        return 0

    timeout = float(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))
    urls = load_urls_env()

    if args.preview:
        bind = (args.preview_bind or os.environ.get("PREVIEW_HTTP_BIND", "127.0.0.1")).strip()
        port = (
            args.preview_port
            if args.preview_port is not None
            else int(os.environ.get("PREVIEW_HTTP_PORT", "8790"))
        )
        session_pv = requests.Session()
        session_pv.headers.update(
            {
                "User-Agent": os.environ.get("HTTP_USER_AGENT", DEFAULT_UA).strip() or DEFAULT_UA,
                "Accept-Language": "ja,en;q=0.9",
            }
        )
        try:
            jobs = fetch_live_listings_snapshot(session_pv, urls, timeout)
        except requests.RequestException as e:
            LOG.error("Preview fetch failed: %s", e)
            return 1
        LOG.info(
            "Structured preview: %d parsed row(s). Open http://%s:%s/  (JSON: http://%s:%s/json) — Ctrl+C to stop.",
            len(jobs),
            bind,
            port,
            bind,
            port,
        )
        if bind in ("0.0.0.0", "::"):
            LOG.warning(
                "PREVIEW is bound to all interfaces; add a firewall rule or use SSH -L %s:127.0.0.1:%s ...",
                port,
                port,
            )
        try:
            serve_structured_preview(bind, port, jobs)
        except OSError as e:
            LOG.error("Could not bind preview server %s:%s — %s", bind, port, e)
            return 1
        return 0

    try:
        listing_webhooks = load_listing_webhook_assignments(urls)
    except ValueError as e:
        LOG.error("%s", e)
        return 1

    unique_hooks = sorted(set(listing_webhooks.values()))
    for hook in unique_hooks:
        try:
            validate_discord_webhook_execute_url(hook)
        except ValueError as e:
            LOG.error("%s (%s)", e, webhook_url_diagnostic(hook))
            return 1

    interval = args.interval if args.interval is not None else int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))
    notify_first = env_bool("NOTIFY_ON_FIRST_RUN", False)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": os.environ.get("HTTP_USER_AGENT", DEFAULT_UA).strip() or DEFAULT_UA,
            "Accept-Language": "ja,en;q=0.9",
        }
    )

    known = load_state(state_path, urls)

    hooks_to_listings: defaultdict[str, list[str]] = defaultdict(list)
    for listing_u, hook in listing_webhooks.items():
        hooks_to_listings[hook].append(listing_u)

    if env_bool("DISCORD_NOTIFY_ON_START", True):
        startup_errors = 0
        for hook, lu_list in sorted(hooks_to_listings.items(), key=lambda kv: kv[0]):
            try:
                discord_notify_monitor_started(
                    hook,
                    once_mode=args.once,
                    interval_seconds=interval,
                    listing_urls_for_this_channel=sorted(lu_list),
                )
            except Exception as e:
                startup_errors += 1
                LOG.warning("Startup Discord notification failed for one channel: %s", e)
        if startup_errors == 0:
            LOG.info("Posted startup notification(s) to %d Discord channel(s).", len(hooks_to_listings))

    try:
        while True:
            try:
                known = run_poll(
                    session,
                    listing_webhooks,
                    urls,
                    state_path,
                    timeout,
                    notify_on_first_poll=notify_first,
                    already_known=known,
                )
            except requests.RequestException as e:
                LOG.error("Network error during poll: %s", e)
            except RuntimeError as e:
                LOG.error("%s", e)

            if args.once:
                break
            LOG.debug("Sleeping %s seconds...", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        LOG.info("Interrupted.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

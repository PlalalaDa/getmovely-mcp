#!/usr/bin/env python3
"""GetMovely MCP server (read-only tools).

What this is
------------
A Model Context Protocol server that lets a person's AI assistant (Claude,
ChatGPT, Cursor, Claude Code, VS Code, Gemini CLI, OpenClaw) query GetMovely's
public property data for England and Wales: addresses and their sale history,
the nine-part address report, homes for sale and homes not for sale by
criteria, sold prices, street price per square metre, price trends, stamp duty,
what is nearby, tenure and construction age, district research figures and
plain-English guides. The hosted server is https://getmovely.co.uk/mcp; this
code is published so anyone can read exactly what it does.

How it works
------------
It calls the GetMovely website's own JSON routes over HTTP and never reads the
database itself, so the website's guards (rate limits, what may be shown about a
home, the 60-row page) stay the single enforcement point. Without that backend
(GM_MCP_BACKEND) the server starts but its tools have nothing to answer from.

* The caller's IP (CF-Connecting-IP, set by Cloudflare in front of the server)
  is forwarded, so the website's per-IP limits apply to the person's assistant,
  not to this process. On top, this process keeps its own daily and per-minute
  budget per caller.
* The website files every call as coming from an assistant (off the
  GetMovely-MCP/ User-Agent), never as a human visitor. Our own monitoring sends
  X-GM-Probe, which keeps it out of the usage numbers; a caller that sends it
  only removes itself from those numbers, and gains no other treatment.
* The call log keeps two kinds of line: a tool call (tool, ok, ms, caller code,
  user agent, protocol, probe, country) and, with `ev: rpc`, every JSON-RPC
  request (method, caller code, client name on the handshake). The caller code
  is an HMAC of the IP under a random key that is replaced every day, so codes
  cannot be matched to an address afterwards, by anyone, us included. Lines
  older than GM_MCP_LOG_KEEP_DAYS are deleted.

What a home's answer may say:
  * whether its Energy Performance Certificate records it as owner-occupied
    (public information from the national register), and the response
    likelihood the website shows beside an address, in the website's words;
  * whether GetMovely has seen it advertised for sale, with what the website's
    own card and report show: asking price, time on the market, the portal, the
    agent and the link to the advert.

What never leaves, on any tool, at any depth (FORBIDDEN_KEYS): internal rank
and tier numbers, internal listing ids, claim codes, e-mail addresses, and the
platform's own activity on an address (letters sent, interest registered).

Layout: this file is the core (server object, backend access, budgets,
logging, the original tools, the human page at GET /mcp). gm_mcp_homes.py,
gm_mcp_areas.py, gm_mcp_content.py and gm_mcp_search.py each expose
register(core) and are loaded at the bottom of this file. They never import
gm_mcp: run as a script this file is __main__, and importing it again under its
own name would build a second server object.

Run: ``python gm_mcp.py`` (port GM_MCP_PORT, default 8096, 127.0.0.1 only).
"""
from __future__ import annotations

import asyncio
import contextvars
import fcntl
import functools
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus

import httpx
from pydantic import Field

from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount

log = logging.getLogger("gm_mcp")

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("GM_MCP_BACKEND", "http://localhost:8091").rstrip("/")
PUBLIC = "https://getmovely.co.uk"
MCP_URL = f"{PUBLIC}/mcp"
PORT = int(os.environ.get("GM_MCP_PORT", "8096"))
VERSION = "0.5.0"
CALL_LOG = os.environ.get("GM_MCP_CALL_LOG", os.path.join(HERE, "mcp_calls.log"))
# Our own clients (the test suite, the maintainer's own assistant, the perf
# sampler) mark themselves with an X-GM-Probe header. The value is forwarded to
# Flask, whose track() drops probe-marked audience events, and written to the
# call log as `probe` so the stats exclude them. Without this the first night's
# 191 test calls became 73 visitor events (design notes, not published), the
# sixth time our own monitoring was counted as traffic. GM_MCP_PROBE_DEFAULT
# marks every call of a process (the in-memory test client sends no headers).
PROBE_DEFAULT = os.environ.get("GM_MCP_PROBE_DEFAULT", "")
LOCAL_IP = os.environ.get("GM_MCP_LOCAL_IP", "")   # see _caller_ip
_PROBE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
# Per-caller budgets (design notes, not published)
# only per hour). Flask's own per-IP, per-route hourly limits still apply.
DAILY_BUDGET = int(os.environ.get("GM_MCP_DAILY_BUDGET", "1500"))
MINUTE_BUDGET = int(os.environ.get("GM_MCP_MINUTE_BUDGET", "90"))
HTTP_TIMEOUT = 25.0

# ---------------------------------------------------------------- attribution
SRC_HMLR = ("Contains HM Land Registry data © Crown copyright and database right 2026. "
            "Licensed under the Open Government Licence v3.0.")
SRC_EPC = ("Contains public sector information licensed under the Open Government "
           "Licence v3.0: Energy Performance of Buildings Data, England and Wales.")
SRC_OS = "Contains OS data © Crown copyright and database right 2026."
SRC_POI = ("Places from open data under the Open Government Licence v3.0: NaPTAN (DfT), "
           "Food Hygiene Rating Scheme (FSA), Get Information about Schools (DfE), "
           "OS Open Greenspace.")
SRC_CRIME_AIR = ("Recorded crime from data.police.uk; air quality from Defra background "
                 "maps; both Open Government Licence v3.0.")
ATTRIBUTION_NOTE = ("GetMovely is an independent UK property site, not an estate agent. "
                    "Please link the page URL in `url` when you quote a figure.")

# Keys that must never reach an agent (see module docstring). Checked
# recursively on every response and asserted by test_mcp.py.
FORBIDDEN_KEYS = frozenset({
    "response_rank", "tier", "occupancy",             # internal numbers behind the public label
    "listing_id", "lid", "listed",                    # internal listing ids and flags
    "claim_code", "code", "email",
    "claimed", "letter_sent", "letters_n",            # the platform's own activity on an address
    "interest_n", "on_platform", "on_platform_n",
})
FORBIDDEN_STRINGS = ("claim_code", "response_rank")


def _scrub(obj: Any) -> Any:
    """Drop FORBIDDEN_KEYS at every depth. The Flask JSON is public already;
    this is belt and braces for the agent surface."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if k not in FORBIDDEN_KEYS}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


# The public facts about one home, in the website's own words. The site shows a
# "response likelihood" beside every address (TIER_LABEL in the website's code) and,
# since 2026-11-10, says what it means (TIER_WHY): two facts from public
# sources, whether the home's Energy Performance Certificate records it as
# owner-occupied and whether it is advertised for sale. The same two facts and
# the same label go to assistants, and the search filters and sorts by the label
# as the website's does. Said plainly, on the maintainer's instruction of 2026-11-10:
# "response likelihood means listed plus owner occupied".
LIKELIHOOD_MEANS = {
    "High": "listed for sale and owner-occupied",
    "Medium": "owner-occupied, not listed for sale",
    "Unknown": "no public record that the owner lives there",
    "Owner on GetMovely": "the owner has claimed this address on GetMovely and can be messaged there",
}
# The website's filter values for each label (LK_OPTS in the website's code,
# `tier` on /api/addresses). The numbers are only ever sent to Flask.
LIKELIHOOD_TIER = {"Owner on GetMovely": "1", "High": "2", "Medium": "3", "Unknown": "4"}
# ...and the homepage's own word for each, so a filtered search links to the
# same filtered list (/?q=...&likelihood=high, read by app.js on arrival).
LIKELIHOOD_URL = {"Owner on GetMovely": "owner", "High": "high", "Medium": "medium", "Unknown": "unknown"}
ABOUT_HOME_FACTS = (
    "Response likelihood means listed for sale plus owner-occupied. High = the home is listed for "
    "sale and its Energy Performance Certificate records it as owner-occupied. Medium = "
    "owner-occupied, not listed for sale. Unknown = no public record that the owner lives there. "
    "owner_occupied is true when the home's latest certificate records it as owner-occupied and "
    "null when there is no such record (null does not mean rented). seen_advertised_for_sale is "
    "true when GetMovely has seen a current public advert for the home, and false when it has not "
    "seen one (adverts are re-read regularly, so a brand-new listing can be missing). Both come "
    "from public sources: the Energy Performance Certificate register and public adverts.")
PORTAL_NOTE = ("GetMovely is independent and is not affiliated with, endorsed by or partnered with "
               "Rightmove, Zoopla or OnTheMarket. The advert link is given so the full listing can be "
               "read at its source.")


def _advert(r: dict) -> dict | None:
    """The current public advert the website links on this home's card, or None.
    Flask attaches it only for an active listing whose link is proven to be on
    the portal's own domain (_attach_portal)."""
    if r.get("portal") and r.get("portal_url"):
        return {"portal": r["portal"], "agent": r.get("portal_agent"), "url": r["portal_url"]}
    return None


def _home_facts(r: dict) -> dict:
    """What public records say about one home, from its card row. Derived from
    the consumer tier exactly as the website's chip is (2 High, 3 Medium), so
    the two surfaces cannot disagree; the numbers themselves stay internal."""
    tier, advert = r.get("tier"), _advert(r)
    label = r.get("likelihood")
    return {"owner_occupied": True if tier in (2, 3) else None,
            "seen_advertised_for_sale": bool(tier == 2 or advert),
            "response_likelihood": label}


def _state(row: dict) -> str:
    """The three-way state the website shows, from the raw flags, before the
    flags themselves are scrubbed. (design notes, not published)
    'available', 'letter_pending' or 'owner_on_platform'."""
    if row.get("claimed"):
        return "owner_on_platform"
    if row.get("letter_sent"):
        return "letter_pending"
    return "available"


def _iso(d: Any) -> str | None:
    """Flask's jsonify writes dates as HTTP dates ('Tue, 04 Aug 2026 00:00:00 GMT');
    agents want ISO."""
    if not d or not isinstance(d, str):
        return d
    try:
        return parsedate_to_datetime(d).date().isoformat()
    except (TypeError, ValueError):
        return d


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


# ---------------------------------------------------------------- links
# The `url` in an answer is the whole traffic mechanism: it is what an assistant
# cites and what the person clicks. Until 0.2.0 most of them did not work.
# /sold-prices/<district> exists for 15 districts and was linked for all 2,300;
# /tools/address-report?akey=... opened an empty form because that page read
# only ?q=; the freehold and age pages read no parameter at all (all measured
# 2026-11-10). The rules now, each covered by crawler/gm_deeplink_check.py:
#   * one home    -> the address report page, by akey (exact), with the address
#                    text as the fallback the page uses if the key has gone;
#   * a search    -> the street's or district's sold-prices page where one
#                    exists (asked of /api/meta), else the homepage search;
#   * a tool page -> only with a parameter that page is known to read.
def _label(*parts: Any) -> str:
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _home_url(akey: str | None, label: str | None = None) -> str:
    if not akey:
        return f"{PUBLIC}/tools/address-report"
    return f"{PUBLIC}/tools/address-report?akey={akey}" + (f"&q={_q(label)}" if label else "")


def _nearby_url(akey: str, radius_m: int, label: str | None = None) -> str:
    return (f"{PUBLIC}/tools/whats-nearby?akey={akey}" + (f"&q={_q(label)}" if label else "")
            + f"&r={radius_m}")


def _trends_url(outcode: str, pt: str = "T", factor: str = "all") -> str:
    return f"{PUBLIC}/tools/price-trends?oc={_q(outcode.split(' ')[0].upper())}&pt={pt}&factor={factor}"


AREA_OCS_TTL = 3600
_AREA_OCS: tuple[float, frozenset[str]] = (0.0, frozenset())


async def _area_outcodes() -> frozenset[str]:
    """Postcode districts that have sold-price pages, from /api/meta, cached an
    hour. A failed read keeps the last good set and retries on the next call."""
    global _AREA_OCS
    at, ocs = _AREA_OCS
    if time.time() - at < AREA_OCS_TTL:
        return ocs
    try:
        r = await _http().get("/api/meta", timeout=5,
                              headers={"User-Agent": f"GetMovely-MCP/{VERSION} meta", "X-GM-Probe": "mcp-meta"})
        ocs = frozenset(str(x).upper() for x in (r.json().get("sold_price_outcodes") or []))
        _AREA_OCS = (time.time(), ocs)
    except (httpx.HTTPError, ValueError):
        pass
    return ocs


async def _search_url(query: str, rows: list[dict], likelihood: str | None = None) -> str:
    """The page for a set of search results: the street's or district's
    sold-prices page when every row is in one district that has pages, else the
    homepage search for the same words (/?q= runs the search on arrival). A
    search filtered by response likelihood always opens the homepage search
    with the same filter on, so the person sees the list the assistant saw."""
    if likelihood in LIKELIHOOD_URL:
        return f"{PUBLIC}/?q={_q(query)}&likelihood={LIKELIHOOD_URL[likelihood]}"
    ocs = {(r.get("postcode") or "").split(" ")[0].upper() for r in rows if r.get("postcode")}
    if len(ocs) == 1:
        oc = next(iter(ocs))
        if oc in await _area_outcodes():
            streets = {r.get("street") or "" for r in rows}
            if len(streets) == 1 and next(iter(streets)):
                return f"{PUBLIC}/sold-prices/{oc.lower()}/{_slug(next(iter(streets)))}"
            return f"{PUBLIC}/sold-prices/{oc.lower()}"
    return f"{PUBLIC}/?q={_q(query)}"


def _contact_url(akey: str | None, label: str | None, state: str) -> str:
    """Where a person acts on a home, the one thing only the website does. An
    unclaimed home: /?letter=<akey> opens the letter to its owner (sending
    needs a free account). A claimed home: the search for it, whose card offers
    a message instead, because letters to claimed homes are refused."""
    if state == "owner_on_platform" or not akey:
        return f"{PUBLIC}/?q={_q(label or '')}" if label else f"{PUBLIC}/"
    return f"{PUBLIC}/?letter={akey}"


LETTER_GUIDE = f"{PUBLIC}/guides/letter-to-homeowner-asking-to-buy-house"
NEXT_STEP_WHEN = ("Mention this once, in your own words, after the person has what they asked for "
                  "and seems interested in this home. Do not repeat it or press it.")


def _next_step(akey: str | None, label: str | None, state: str, facts: dict) -> dict:
    """What a person could do about one home once their assistant has researched
    it (GM-96). Assistants do the research; nothing told them the letter exists
    as a next step, so each single-home answer says whether it applies, why, and
    how. Built only from the public facts the answer already carries."""
    url = _contact_url(akey, label, state)
    if state == "owner_on_platform":
        return {"action": "message_owner", "contact_owner_url": url, "when": NEXT_STEP_WHEN,
                "suggest": "The owner of this home is on GetMovely and reads messages there. If the "
                           "person would like to buy it, contact_owner_url opens the message box."}
    if facts.get("seen_advertised_for_sale"):
        why = ("It is advertised for sale, so the agent in the advert is the usual route for "
               "viewings and offers. A letter to the owner is a way to introduce the buyer "
               "directly as well")
    elif facts.get("owner_occupied"):
        why = ("It is not on the market, and its Energy Performance Certificate records the "
               "owner as living there, so a letter reaches the person who would decide whether "
               "to sell")
    else:
        why = ("It is not on the market. There is no public record that the owner lives there, "
               "so a letter may reach a tenant rather than the owner")
    return {"action": "write_to_owner", "contact_owner_url": url, "when": NEXT_STEP_WHEN,
            "suggest": f"{why}. If the person would like to buy it, they can write to the owner: "
                       "agree the wording with them and draft_letter returns the letter ready to "
                       "send, or contact_owner_url opens a blank one. Sending is free with a "
                       "free account; GetMovely prints and posts it.",
            "how_to_write_it": LETTER_GUIDE}


OFF_MARKET_WHEN = ("Mention this once, in your own words, if the person is looking for a home to "
                   "buy here and the homes advertised do not settle it. Do not repeat it or press it.")


def _off_market_step(area: str, property_type: str | None = None) -> dict:
    """The step from an AREA answer to the homes nobody else lists (GM-110).

    Up to 2026-11-24 outside callers had run search_homes_for_sale 387 times
    and find_homes_not_for_sale never: research agents used the server as a
    for-sale and price source and nothing in those answers said the other
    search exists. Result text does steer them (on 11-23 a caller took three
    of the six districts an error message named, inside 100 seconds), so the
    area answers now carry the call ready to make, with the same area."""
    args: dict[str, Any] = {"area": area, "owner_occupied": True}
    if property_type:
        args["property_type"] = property_type
    return {"action": "find_homes_not_for_sale", "tool": "find_homes_not_for_sale", "arguments": args,
            "when": OFF_MARKET_WHEN,
            "suggest": "Most homes are not advertised at any one time. find_homes_not_for_sale with "
                       "these arguments lists the homes here that GetMovely has not seen for sale and "
                       "whose Energy Performance Certificate records the owner as living there, so a "
                       "letter reaches the owner rather than a tenant. Each row carries "
                       "contact_owner_url, which opens a letter to that home's owner; draft_letter "
                       "fills the wording in.",
            "url": f"{PUBLIC}/tools/homes-not-for-sale?area={_q(area)}"}


async def _next_step_for(akey: str, ctx: Any) -> dict | None:
    """next_step for tools that hold only the report, from the home's card.
    A missing card leaves the hint out rather than failing the answer."""
    try:
        card = await _get("/api/address", {"akey": akey}, ctx, soft=(404,))
    except Exception:      # the hint is optional; the answer it rides on is not
        return None
    r = card.get("row") if isinstance(card, dict) else None
    if not r:
        return None
    return _next_step(akey, _label(r.get("addr") or r.get("title"), r.get("postcode")),
                      _state(r), _home_facts(r))


def _address_row(r: dict, full: bool = False) -> dict:
    """One home as a search row; `full` adds what a single-home answer carries
    (the advert, what the likelihood label means, where to write to the owner).
    Rows stay lean: a page is sixty of them."""
    label = _label(r.get("addr") or r.get("title"), r.get("postcode"))
    state = _state(r)
    out = {
        "akey": r.get("akey"),
        "address": r.get("addr") or r.get("title"),
        "unit": r.get("unit") or None,
        "street": r.get("street"),
        "postcode": r.get("postcode"),
        "last_sale_price_gbp": r.get("last_sale"),
        "last_sale_date": _iso(r.get("last_date")),
        "sales_on_record": r.get("n_sales"),
        "tenure_at_last_sale": r.get("tenure"),
        **_home_facts(r),
        "lat": r.get("lat"), "lon": r.get("lon"),
        "state": state,
        "url": _home_url(r.get("akey"), label),
    }
    if full:
        advert = _advert(r)
        out["response_likelihood_means"] = LIKELIHOOD_MEANS.get(out["response_likelihood"])
        if advert:
            out["advert"] = advert
            out["portal_note"] = PORTAL_NOTE
        out["contact_owner_url"] = _contact_url(r.get("akey"), label, state)
        out["next_step"] = _next_step(r.get("akey"), label, state, _home_facts(r))
    return out


# ---------------------------------------------------------------- budgets
class Budget:
    """In-memory per-caller budgets. One process, so a dict is the truth."""

    def __init__(self) -> None:
        self.day: dict[str, tuple[str, int]] = {}
        self.minute: dict[str, tuple[int, int]] = {}

    def take(self, key: str, now: float | None = None) -> str | None:
        """Count one call for `key`; a message when it is over budget. `now` is
        injectable so the tests do not depend on where in the minute they run."""
        now = time.time() if now is None else now
        today = date.fromtimestamp(now).isoformat()
        d_day, n_day = self.day.get(key, (today, 0))
        if d_day != today:
            n_day = 0
        if n_day >= DAILY_BUDGET:
            return f"daily budget of {DAILY_BUDGET} calls reached; try again tomorrow"
        m = int(now // 60)
        m_min, n_min = self.minute.get(key, (m, 0))
        if m_min != m:
            n_min = 0
        if n_min >= MINUTE_BUDGET:
            return f"more than {MINUTE_BUDGET} calls in a minute; slow down"
        self.day[key] = (today, n_day + 1)
        self.minute[key] = (m, n_min + 1)
        if len(self.minute) > 50_000:      # keep the dicts bounded
            self.minute = {k: v for k, v in self.minute.items() if v[0] == m}
        if len(self.day) > 50_000:         # an IP-rotating scanner must not grow memory until restart
            self.day = {k: v for k, v in self.day.items() if v[0] == today}
        return None


BUDGET = Budget()


def _caller_ip(ctx: Context | None) -> str:
    """Who is calling, for the budgets and for Flask's per-IP limits: the
    address Cloudflare saw. A request with none came from this machine;
    GM_MCP_LOCAL_IP (set by the test suite to a fresh benchmark-range address
    per run) gives those a key of their own, so a suite that now asks for a
    hundred reports cannot use up the 60 an hour Flask allows one caller and
    then fail its own next run."""
    v = _hdr(ctx, "cf-connecting-ip", "x-forwarded-for")
    return v.split(",")[0].strip() if v else (LOCAL_IP or "local")


def _ua(ctx: Context | None) -> str:
    return _hdr(ctx, "user-agent")[:160]


def _probe_value(raw: str) -> str:
    """The probe marker as logged and forwarded: the header value when it is a
    plain token, else '1'; the process default when the request has none."""
    raw = (raw or "").strip()
    if raw:
        return raw if _PROBE_RE.match(raw) else "1"
    return PROBE_DEFAULT


def _probe(ctx: Context | None) -> str:
    return _probe_value(_hdr(ctx, "x-gm-probe"))


def _country_value(raw: str) -> str:
    """Cloudflare's CF-IPCountry, as the website stores it: two letters, never
    XX (unplaced) or T1 (Tor). Coarse, not an identifier."""
    cc = (raw or "").strip().upper()[:2]
    return cc if len(cc) == 2 and cc.isalpha() and cc != "XX" else ""


# The caller code in the call log tells one caller from another within a day
# and nothing else. Until 2026-11-13 it was sha256(date|ip), which anyone who
# had the log could turn back into an address by trying addresses. Now the key
# is 32 random bytes that live in SALT_FILE (outside git, mode 600) for one day
# and are overwritten at the first call of the next, as the website does for its
# visitor code (gm_app._daily_salt): yesterday's key is gone, so yesterday's
# codes cannot be matched to an address by anyone, us included. The file is
# shared so the service and a test run on the same day agree; the lock keeps two
# processes from each writing a key at midnight.
SALT_FILE = os.environ.get("GM_MCP_SALT_FILE", os.path.join(HERE, ".caller_salt"))
# Call-log lines older than this are deleted when the day's key is renewed.
# 90 days is the period proposed in the privacy policy;
# the privacy policy must state whatever this is set to.
LOG_KEEP_DAYS = int(os.environ.get("GM_MCP_LOG_KEEP_DAYS", "90"))
_SALT = {"day": "", "key": b""}


def _daily_key() -> bytes:
    today = date.today().isoformat()
    if _SALT["day"] == today:
        return _SALT["key"]
    key = b""
    try:
        with open(SALT_FILE + ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(SALT_FILE, encoding="ascii") as f:
                    day, hexkey = f.read().split()
                key = bytes.fromhex(hexkey) if day == today else b""
            except (OSError, ValueError):
                key = b""
            if len(key) != 32:
                key = secrets.token_bytes(32)
                tmp = SALT_FILE + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    f.write(f"{today} {key.hex()}\n")
                os.replace(tmp, SALT_FILE)
                _prune_call_log(today)
    except OSError:
        # No writable file: a key for this process only. Codes still cannot be
        # reversed; they just differ between processes that day.
        log.warning("caller salt file unusable; using a process-local key")
        key = key if len(key) == 32 else secrets.token_bytes(32)
    _SALT.update(day=today, key=key)
    return key


def _prune_call_log(today: str) -> None:
    """Drop call-log lines older than LOG_KEEP_DAYS. Runs once a day, under the
    salt lock, when the key is renewed. Lines are dated by their own `ts`; a
    line that cannot be read as one is dropped with the old ones."""
    if LOG_KEEP_DAYS <= 0:
        return
    cutoff = (date.fromisoformat(today) - timedelta(days=LOG_KEEP_DAYS)).isoformat()
    try:
        with open(CALL_LOG, encoding="utf-8") as f:
            lines = f.readlines()
        keep = [ln for ln in lines if ln[8:18] >= cutoff and ln.startswith('{"ts": "')]
        if len(keep) == len(lines):
            return
        tmp = CALL_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, CALL_LOG)
        log.info("call log pruned: %d lines older than %s removed", len(lines) - len(keep), cutoff)
    except OSError:
        log.warning("call log not pruned")


def _ip_hash(ip: str) -> str:
    return hmac.new(_daily_key(), ip.encode(), hashlib.sha256).hexdigest()[:12]


def _write_log(rec: dict) -> None:
    try:
        with open(CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("call log unwritable")


def _log_call(tool: str, ctx: Context | None, ok: bool, ms: int, note: str = "", backend: int = 0) -> None:
    """One JSONL line per tool call. `probe` names our own client when the call
    is ours (the stats reader excludes those); `country` is Cloudflare's
    two-letter placement of the caller, the same coarse field the website keeps;
    `backend` is how many Flask requests the call made (the composite tools make
    several). Arguments are never logged: they are addresses people typed."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, "ok": ok, "ms": ms,
           "caller": _ip_hash(_caller_ip(ctx)), "ua": _ua(ctx),
           "proto": getattr(ctx, "protocol_version", None) if ctx is not None else None,
           "probe": _probe(ctx) or None, "country": _country_value(_hdr(ctx, "cf-ipcountry")) or None,
           "backend": backend}
    if note:
        rec["note"] = note[:200]
    _write_log(rec)


class ToolFailure(ToolError):
    """A message the model should read (becomes is_error=True with this text)."""


# One log line per TOOL call, however many backend requests it makes. The
# composite tools (compare_homes, area_overview, price_check) fan out to several
# Flask routes; logging per request, as 0.1.x did, would have turned one
# question into six "calls" in every number read off the log. logged() wraps a
# tool and opens a scope; _get() counts and annotates into it.
_CALL: contextvars.ContextVar[dict | None] = contextvars.ContextVar("gm_mcp_call", default=None)


def logged(tool: str):
    """Decorator, placed under @mcp.tool: time the call, log it once with its
    outcome. functools.wraps keeps the signature the SDK builds the schema from."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx")
            scope = {"backend": 0, "note": ""}
            token = _CALL.set(scope)
            t0 = time.monotonic()
            try:
                out = await fn(*args, **kwargs)
            except ToolFailure as e:
                _log_call(tool, ctx, False, int((time.monotonic() - t0) * 1000),
                          scope["note"] or f"refused: {str(e)[:80]}", scope["backend"])
                raise
            except Exception as e:
                _log_call(tool, ctx, False, int((time.monotonic() - t0) * 1000),
                          f"error {type(e).__name__}", scope["backend"])
                raise
            finally:
                _CALL.reset(token)
            _log_call(tool, ctx, True, int((time.monotonic() - t0) * 1000), "", scope["backend"])
            return out
        return wrapper
    return deco


def _note(text: str) -> None:
    scope = _CALL.get()
    if scope is not None and not scope["note"]:
        scope["note"] = text


def _q(query: str) -> str:
    return quote_plus(query)


def _hdr(ctx: Context | None, *names: str) -> str:
    h = (getattr(ctx, "headers", None) if ctx is not None else None) or {}
    for n in names:
        for k in (n, n.lower(), n.title()):
            v = h.get(k) if hasattr(h, "get") else None
            if v:
                return v
    return ""


# ---------------------------------------------------------------- backend
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BACKEND, timeout=HTTP_TIMEOUT,
                                    headers={"Accept": "application/json"})
    return _client


def _backend_headers(ctx: Context | None) -> dict:
    """What every backend request carries. The User-Agent prefix is what Flask's
    _attribution() reads to file the event as source `mcp` (an assistant acting
    for a person, never a human visitor)."""
    headers = {"User-Agent": f"GetMovely-MCP/{VERSION} (+{MCP_URL}; via {_ua(ctx) or 'unknown client'})"}
    ip = _caller_ip(ctx)
    if ip != "local":
        headers["CF-Connecting-IP"] = ip
    probe = _probe(ctx)
    if probe:
        # Our own client: Flask's track() drops the audience event, as it does
        # for every other probe. Forwarded as-is; the website lets any HTTP
        # caller opt out of being counted the same way, so this adds no new
        # hole, and the call stays in our own log with probe set.
        headers["X-GM-Probe"] = probe
    cc = _country_value(_hdr(ctx, "cf-ipcountry"))
    if cc:
        # So an assistant's tool calls carry the caller's country like a page
        # view does; without it MCP rows were the only country-less events.
        headers["CF-IPCountry"] = cc
    return headers


async def _get(path: str, params: dict, ctx: Context | None, *, soft: tuple[int, ...] = ()) -> dict:
    """GET a Flask JSON route: forwards the real caller IP, names this process,
    takes one unit of this process's budget per request, and reports into the
    tool's log scope. A status listed in `soft` returns its JSON body instead
    of raising, for callers that read `ok` and the detail themselves."""
    scope = _CALL.get()
    why = BUDGET.take(_caller_ip(ctx))
    if why:
        _note("budget")
        raise ToolFailure(f"GetMovely MCP: {why}.")
    if scope is not None:
        scope["backend"] += 1
    try:
        r = await _http().get(path, params={k: v for k, v in params.items() if v not in (None, "")},
                              headers=_backend_headers(ctx))
    except httpx.HTTPError as e:
        _note(f"backend {type(e).__name__}")
        raise ToolFailure("GetMovely is not answering right now; try again in a minute.") from e
    if r.status_code == 429:
        _note("429")
        raise ToolFailure("GetMovely rate limit for this caller reached on this tool; wait an hour.")
    try:
        data = r.json()
    except ValueError:
        _note(f"non-json {r.status_code}")
        raise ToolFailure(f"GetMovely returned an unexpected response ({r.status_code}).")
    if r.status_code in soft:
        return data
    if r.status_code >= 400 or data.get("ok") is False:
        _note(str(data.get("error") or r.status_code))
        raise ToolFailure(f"GetMovely: {data.get('error') or 'request failed'} ({r.status_code}).")
    return data


async def _get_text(path: str, ctx: Context | None = None) -> str:
    r = await _http().get(path, headers=_backend_headers(ctx))
    r.raise_for_status()
    return r.text


async def _gather(coros: list, limit: int = 4) -> list:
    """Run backend calls side by side, at most `limit` at once (Flask's
    connection pool ran dry under load on 2026-11-04; a tool must not be the
    next cause). Exceptions come back in place, for the caller to sort."""
    sem = asyncio.Semaphore(limit)

    async def one(c):
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros), return_exceptions=True)


# ---------------------------------------------------------------- the server
INSTRUCTIONS = f"""GetMovely is an independent UK property website (getmovely.co.uk) covering
every home in England and Wales: sold-price history from HM Land Registry, freehold or
leasehold, Energy Performance Certificate facts, whether the certificate records the home
as owner-occupied, whether it has been seen advertised for sale, a nine-part address
report, price per square metre, what is nearby, research figures by district and
plain-English guides. It also lets a prospective buyer write to the owner of a specific
home, on the market or not, on the website; answers about one home carry the link to that
page.

Which tool for which question:
- Looking for a home to buy ("find me a 3-bed under 400k in BS7"): search_homes_for_sale
  lists the homes advertised for sale in a district, town or street that fit a price range,
  bedrooms, property type and floor area, with the asking price per square metre, days on
  the market and the link to each advert. When the person describes the home in their
  own words ("riverside flat with a balcony", "cottage that needs work"):
  describe_homes_for_sale ranks the adverts by how well their own text matches, takes a
  whole city as the area, and filters by tags the adverts state (chain free, probate,
  needs modernisation, waterside and twenty more).
- Homes that are NOT on the market ("which houses on this street could I write to",
  "owner-occupied houses over 120 m2 here that have not sold since 2005"):
  find_homes_not_for_sale searches them by type, size, tenure, energy rating, last sale and
  whether the owner lives there. Each row carries the link that opens a letter to its owner.
- What sold ("what sold near here this year"): recent_sales lists sold prices in a district,
  postcode or street, newest first, with price per square metre.
- "How much stamp duty": stamp_duty, for England (SDLT) and Wales (LTT), for a home mover, a
  first-time buyer or an additional property.
- When the person has agreed the wording of a letter to an owner: draft_letter returns a
  link that opens that letter on GetMovely with the text filled in. The person reads it and
  sends it there; the tool itself sends nothing.
- A question about one known address starts with search_addresses, which turns a street, postcode or
  town into addresses and their `akey`; every per-address tool takes an `akey`. If the
  result lists `streets`, the name exists in several places; adding a town or postcode
  narrows it to one.
- One home: address_report is the full picture, including whether it is for sale and
  whether the owner lives there; get_address is the short card with the same two facts;
  sale_history and energy_performance are the short versions of two of its parts;
  price_check sets a price against the district and the street; negotiation_brief is the
  evidence for an offer on a home that is for sale (asking-price cuts GetMovely has seen,
  time on the market, asking price against district, street and its own last sale); buyer_checks
  answers "anything I should worry about": flood risk bands, nearest schools with their
  last published Ofsted grade, crime, air, weak parts of the building and tenure, with the
  questions they raise; compare_homes puts two to
  four homes side by side; whats_nearby, tenure_check and house_age answer one question each.
  whats_nearby also takes up to 50 homes at once in `akeys`: one call covers a list of homes.
- An area: area_overview for one postcode district, compare_areas for several,
  price_trends for what tenure, energy rating, new build or size are worth there,
  district_statistics for leasehold share, how long people stay and home sizes by local
  authority district.
- A listing that hides its address but shows past sales: find_address_from_sale_history.
- The three search tools take an `area`: a postcode district ('BS7'), a postcode, a town, or
  a street with its district or town ('Elm Road, BS7'). A large town must be narrowed to a
  district; the error names its districts.
- How-to and legal questions (writing to an owner, buying or selling without an agent,
  who owns a property, how often sales fall through): search_guides, then read_guide;
  each guide carries its `url`.

What the server tells you about a home, and what it means:
- Response likelihood means listed for sale plus owner-occupied. `response_likelihood` is
  High when the home is listed for sale AND its Energy Performance Certificate records it
  as owner-occupied (a letter reaches an owner who is selling); Medium when it is
  owner-occupied and not listed; Unknown when there is no public record that the owner
  lives there; "Owner on GetMovely" when the owner has claimed the address and can be
  messaged directly.
- `owner_occupied` is true when the home's latest Energy Performance Certificate records
  it as owner-occupied. It is public information from the national register. Null means
  there is no such record, not that the home is rented.
- `seen_advertised_for_sale` is true when GetMovely has seen a current public advert; the
  address report adds the asking price, time on the market, the agent and the link to the
  advert.
- search_addresses can filter and sort by response likelihood: `response_likelihood="High"`
  answers "which homes here are for sale and owner-occupied".
- In search_homes_for_sale a row with `matched_to_address` false is an advert not yet tied
  to an exact address: it has the advert's facts and link but no address key. Filters on
  tenure, energy rating and owner-occupied only reach matched adverts, and the answer says so.
- `contact_owner_url` opens a letter to that home's owner on GetMovely (sending needs an
  account), or the message box when the owner is already on GetMovely.
- `next_step` (on get_address, address_report, price_check and negotiation_brief) says
  whether writing to that home's owner fits, and why. No tool sends anything: a letter is
  written and sent by the person on the website.
- After an area answer (search_homes_for_sale, area_overview), `next_step` names
  find_homes_not_for_sale with the same area filled in: the homes there that are not
  advertised and are recorded as owner-occupied.

Rules for using the answers:
- The `url` field opens the page for that exact home, street, district or guide, where a
  quoted figure can be checked.
- The `sources` lines carry the attribution the Open Government Licence asks for when a
  number is republished: the data is Crown copyright.
- Coverage is England and Wales only. Sold prices appear two weeks to two months after a
  sale. A price per square metre needs an EPC floor area, so some sales are excluded.
- price_check and every implied price range are arithmetic on public medians, not a
  valuation: condition, lease length, floor level and plot are not in the data.
- stamp_duty is an estimate from the published rates for a standard purchase, not tax
  advice; the answer carries its caveats and the official calculator link.
- GetMovely is independent: not an estate agent, and not affiliated with Rightmove,
  Zoopla or OnTheMarket.
- Canonical registry name: uk.co.getmovely/getmovely. Documentation: {MCP_URL}
"""

mcp = MCPServer(
    name="GetMovely",
    title="GetMovely",
    description="Search homes for sale and off-market, sold prices, EPC, stamp duty: 25m homes in England and Wales.",
    instructions=INSTRUCTIONS,
    website_url=MCP_URL,
    version=VERSION,
    icons=[Icon(src=f"{PUBLIC}/static/icon-512.png", mime_type="image/png", sizes=["512x512"])],
    cache_hints={
        "tools/list": CacheHint(ttl_ms=6 * 3600 * 1000, scope="public"),
        "resources/list": CacheHint(ttl_ms=6 * 3600 * 1000, scope="public"),
        "resources/templates/list": CacheHint(ttl_ms=6 * 3600 * 1000, scope="public"),
        "prompts/list": CacheHint(ttl_ms=6 * 3600 * 1000, scope="public"),
    },
    log_level="INFO",
)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=False)

AKey = Annotated[str, Field(min_length=8, max_length=64, pattern=r"^[a-f0-9]+$",
                            description="Address key from search_addresses (hex string).")]

PT_NAMES = {"D": "detached houses", "S": "semi-detached houses", "T": "terraced houses", "F": "flats"}

SEARCH_MAX_OFFSET = 1140   # what 20 pages of 60 reached before `limit` existed
FUZZY_ROWS = 10            # near-misses returned when nothing matches exactly
SALE_PAIR_RE = re.compile(r"\d{4,9}:(19|20)\d\d")


# Every link to a GetMovely page that leaves this server carries utm_source=mcp.
# Flask already files the server's own API calls as source `mcp` with ai_fetch
# set (an assistant read); a person who then clicks the link in the assistant's
# answer arrives as source `mcp` WITHOUT it. So the AI panel's existing split
# (assistant reads / people) answers the question the server exists to answer:
# does it send anyone. Tagged by key, after scrubbing, so no tool can forget:
# `url`, anything ending `_url`, `guide`, `sold_prices_by_street`, and the
# values of `study_pages`. Links inside guide Markdown and third-party
# references are left alone.
_LINK_KEYS = frozenset({"url", "guide", "sold_prices_by_street"})


def _utm(url: str) -> str:
    if not url.startswith(PUBLIC + "/") or url.startswith(MCP_URL) or "utm_source=" in url:
        return url
    base, _, frag = url.partition("#")
    return base + ("&" if "?" in base else "?") + "utm_source=mcp" + (f"#{frag}" if frag else "")


def _tag_links(obj: Any, key: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: _tag_links(v, "url" if key == "study_pages" else k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_tag_links(v, key) for v in obj]
    if isinstance(obj, str) and key and (key in _LINK_KEYS or key.endswith("_url")):
        return _utm(obj)
    return obj


def _finish(obj: dict) -> dict:
    """What every tool returns through: forbidden keys out, links tagged."""
    return _tag_links(_scrub(obj))


def _ok(**kw: Any) -> dict:
    kw.setdefault("sources", [SRC_HMLR])
    kw["note"] = ATTRIBUTION_NOTE
    return _finish(kw)


def _report_label(rep: dict) -> str:
    """'Flat 5 41 Denning Road NW3 1ST' from a report's title, unit and postcode."""
    return _label(rep.get("unit_label"), rep.get("title"), rep.get("postcode"))


def _report_address(rep: dict) -> str:
    """'Flat 5, 41 Denning Road' as a person would write it."""
    return ", ".join(x for x in [rep.get("unit_label"), rep.get("title")] if x)


def _tiles(rep: dict) -> dict[str, dict]:
    return {t.get("key"): t for t in rep.get("tiles") or [] if isinstance(t, dict)}


def _sale_pairs(pairs: list[str] | None) -> str | None:
    if not pairs:
        return None
    for p in pairs:
        if not SALE_PAIR_RE.fullmatch(p.strip()):
            raise ToolFailure(f"Each sale is 'price:year', for example '450000:2019'; got {p!r}.")
    return ",".join(p.strip() for p in pairs)


@mcp.tool(title="Search addresses", annotations=READ)
@logged("search_addresses")
async def search_addresses(
    query: Annotated[str, Field(min_length=2, max_length=160,
                                description="Street, postcode, outcode, town or full address, "
                                            "as a person would type it: 'Merton Road, SW19', "
                                            "'BS7 8', 'Ash Street, Salford M6', '41 Denning Road NW3'.")],
    sale_pairs: Annotated[list[str] | None, Field(max_length=6, description="Filter to homes whose sale history contains these sales, each as 'price:year', e.g. ['450000:2019']. This is how to find the exact address behind a listing or an advert that shows past sales.")] = None,
    response_likelihood: Annotated[Literal["High", "Medium", "Unknown", "Owner on GetMovely"] | None, Field(
        description="Only homes with this response likelihood. 'High' = listed for sale and "
                    "owner-occupied (the homes where a letter reaches an owner who is selling). "
                    "'Medium' = owner-occupied, not listed for sale. 'Unknown' = no public record "
                    "that the owner lives there. 'Owner on GetMovely' = the owner has claimed the "
                    "address. Leave out for all homes.")] = None,
    sort: Annotated[Literal["recent", "likelihood", "price_desc", "price_asc", "addr"], Field(
        description="'recent' newest sale first (default); 'likelihood' High first, then Medium, "
                    "then the rest; 'price_desc' / 'price_asc' by last sale price; 'addr' by address.")] = "recent",
    page: Annotated[int, Field(ge=1, le=60, description="Page number; `limit` addresses per page.")] = 1,
    limit: Annotated[int, Field(ge=1, le=60, description="Addresses per page: 20 by default, up to 60. "
                                                         "Ask for fewer when one home is wanted.")] = 20,
    ctx: Context = None,
) -> dict:
    """Find homes in England and Wales by street, postcode, town or address. Each row has
    the home's last sale, freehold or leasehold, whether it is owner-occupied, whether it
    is listed for sale, and its response likelihood (High = listed for sale and
    owner-occupied, Medium = owner-occupied, Unknown = no record). Filter with
    `response_likelihood` to answer "which homes on this street are owner-occupied and for
    sale", or sort by it. Returns 20 per page (`limit`, up to 60), newest sale first by default.
    If the street name exists in several places the answer lists the places as `streets`
    instead of rows; a search with the town or postcode added returns the homes.
    When nothing matches exactly the answer says so (`exact_match` false) and gives at most
    ten of the nearest names, which may be in other towns: Scotland and Northern Ireland are
    not covered."""
    offset = (page - 1) * limit
    if offset > SEARCH_MAX_OFFSET:
        raise ToolFailure("That is further than this search pages. Narrow it with a street, a postcode "
                          "or the response_likelihood filter.")
    data = await _get("/api/addresses", {"q": query, "pairs": _sale_pairs(sale_pairs),
                                         "tier": LIKELIHOOD_TIER.get(response_likelihood),
                                         "sort": "rank" if sort == "likelihood" else sort,
                                         "offset": offset}, ctx)
    if data.get("streets") is not None:
        streets = data["streets"]
        return _ok(ambiguous=True, query=query,
                   hint="This street name exists in several places. Ask which, or search again with the town, county or postcode district added.",
                   streets=[{"street": (s.get("street") or "").title(), "outcode": s.get("outcode"),
                             "county": (s.get("county") or "").title() or None, "homes": s.get("n")}
                            for s in streets[:40]],
                   places_total=len(streets), homes_total=data.get("homes"),
                   url=f"{PUBLIC}/?q={_q(query)}")
    if data.get("need_filter"):
        raise ToolFailure("Add a street, postcode or town; the whole address universe cannot be listed.")
    raw = data.get("rows", [])
    if data.get("fuzzy"):
        # Nothing matched what was typed, so the website fell back to the nearest
        # names anywhere. A page of sixty of those answered "Princes Street,
        # Edinburgh" with 41 KB of homes in Swindon; a few, plainly labelled, is
        # all a near-miss is worth. No total: the count of near-misses means nothing.
        raw = raw[:FUZZY_ROWS] if page == 1 else []
        return _ok(message="No exact match. Coverage is England and Wales only. Nearest by name:",
                   query=query, exact_match=False, fuzzy_match=True,
                   response_likelihood_filter=response_likelihood,
                   hint="These are similar names, possibly in other towns. Check the spelling, or add "
                        "a postcode or its first half.",
                   rows=[_address_row(r) for r in raw], about_these_fields=ABOUT_HOME_FACTS,
                   url=f"{PUBLIC}/?q={_q(query)}",
                   sources=[SRC_HMLR, SRC_EPC, SRC_OS])
    total = data.get("total")   # the website counts on the first page only
    more = (total > offset + limit) if isinstance(total, int) else len(raw) > limit or len(raw) == 60
    raw = raw[:limit]
    return _ok(query=query, response_likelihood_filter=response_likelihood,
               total=data.get("total"), total_label=data.get("total_label"),
               page=page, per_page=limit, more=more, exact_match=True, fuzzy_match=False,
               rows=[_address_row(r) for r in raw], about_these_fields=ABOUT_HOME_FACTS,
               url=await _search_url(query, raw, response_likelihood),
               sources=[SRC_HMLR, SRC_EPC, SRC_OS])


@mcp.tool(title="Address card", annotations=READ)
@logged("get_address")
async def get_address(akey: AKey, ctx: Context = None) -> dict:
    """One home's card, the quick answer to "is this house for sale" and "does the owner
    live there": full address, last sale, freehold or leasehold, whether it is
    owner-occupied (from its Energy Performance Certificate), whether it is listed for sale
    (with the portal, the agent and the link to the advert), its response likelihood (High
    = listed for sale and owner-occupied), and the link that opens a letter to its owner
    on GetMovely.

    Use it for those yes/no facts about a home you already have an `akey` for (from
    search_addresses or any row another tool returned). For prices, energy, the street and
    the area use address_report (everything) or the single-topic tools. An `akey` comes
    only from another tool's answer; an unknown one fails with "address not found". A field that is null means no
    public record, not "no": `owner_occupied` null is not the same as false."""
    data = await _get("/api/address", {"akey": akey}, ctx)
    r = data["row"]
    return _ok(**_address_row(r, full=True), about_these_fields=ABOUT_HOME_FACTS,
               sources=[SRC_HMLR, SRC_EPC, SRC_OS])


@mcp.tool(title="Address report (nine tiles)", annotations=READ)
@logged("address_report")
async def address_report(akey: AKey, ctx: Context = None) -> dict:
    """Everything GetMovely knows about one home in one call. `home` gives the facts in
    brief: freehold or leasehold, whether it is owner-occupied (from its Energy
    Performance Certificate), whether it is listed for sale, and its response likelihood
    (High = listed for sale and owner-occupied). `tiles` is the nine-part report the
    website shows: sold-price
    history, building type and size, energy rating with costs and improvements, local
    market (price per m2 nearby), area price trend, market history (asking price, time on
    the market, the agent and the link to the advert when it is for sale), nearby
    essentials, recorded crime and air quality. `contact_owner_url` opens a letter to its
    owner. About 15 KB: use get_address, sale_history, energy_performance or price_check
    when one part is enough. Limited to 60 reports per hour per caller, a limit
    sale_history, energy_performance, price_check and compare_homes share."""
    got = await _gather([_get("/api/address-report", {"akey": akey}, ctx),
                         _get("/api/address", {"akey": akey}, ctx, soft=(404,))])
    data, card = got
    if isinstance(data, Exception):
        raise data
    label = _report_label(data)
    data["address"] = _report_address(data)
    row = card.get("row") if isinstance(card, dict) else None
    if row:
        state = _state(row)
        facts = _home_facts(row)
        data["home"] = {"tenure_at_last_sale": row.get("tenure"), **facts,
                        "response_likelihood_means": LIKELIHOOD_MEANS.get(facts["response_likelihood"]),
                        "state": state}
        data["contact_owner_url"] = _contact_url(akey, label, state)
        data["next_step"] = _next_step(akey, label, state, facts)
        data["about_these_fields"] = ABOUT_HOME_FACTS
    if any((t.get("detail") or {}).get("portal") for t in data.get("tiles") or [] if isinstance(t, dict)):
        data["portal_note"] = PORTAL_NOTE
    data["url"] = _home_url(akey, label)
    return _ok(**data, sources=[SRC_HMLR, SRC_EPC, SRC_OS, SRC_POI, SRC_CRIME_AIR])


@mcp.tool(title="Street price per square metre", annotations=READ)
@logged("street_price_per_m2")
async def street_price_per_m2(akey: AKey, ctx: Context = None) -> dict:
    """Recent Land Registry sales near one home with their EPC floor area, as pounds per
    square metre and per square foot by year. Scope is the home's own postcode when it has
    enough sized sales, otherwise the whole street; `scope` says which. Market-value
    (category A) sales only.

    Use it for "what do homes on this street go for per square metre". To judge one
    price or an asking price use price_check, which sets the street beside the district;
    for a list of the sales themselves use recent_sales with the street as `area`; for a
    whole district use area_overview. Takes an `akey` from search_addresses or any row
    another tool returned. A street with no sale that has a floor area on record returns
    an empty `years`; that means too little evidence, not a price of zero."""
    data = await _get("/api/street-ppsm", {"akey": akey}, ctx)
    data["url"] = _home_url(akey)
    data["study_url"] = f"{PUBLIC}/research/price-per-square-metre"
    return _ok(**data, sources=[SRC_HMLR, SRC_EPC])


@mcp.tool(title="Price trends by factor", annotations=READ)
@logged("price_trends")
async def price_trends(
    outcode: Annotated[str, Field(min_length=2, max_length=8,
                                  description="Postcode district (outcode) such as 'SW19', 'BS7', 'M6'. A full postcode is accepted; only the district is used.")],
    property_type: Literal["D", "S", "T", "F"] = "T",
    factor: Literal["all", "tenure", "newbuild", "epc", "size"] = "all",
    ctx: Context = None,
) -> dict:
    """What freehold or leasehold, new build or resale, energy rating and floor area are
    worth per square metre in one postcode district, for one property type (D detached,
    S semi-detached, T terraced, F flat), by whole calendar year, against the national
    picture. Property type is always held constant because it is the confounder that
    makes the naive comparison lie. Every figure is a median of at least a minimum
    number of sales; no per-address data. For a plain summary of a district across all
    its property types use area_overview.

    Use it for "is a leasehold flat cheaper here", "what is an A-rated house worth over a
    D", "do bigger homes cost less per m2". For several districts use compare_areas; for
    one home use price_check. When a district has too few sales of that type the tool
    fails and names the property types it does have, so ask again with one of those."""
    data = await _get("/api/price-trends", {"oc": outcode, "pt": property_type, "factor": factor},
                      ctx, soft=(404,))
    if data.get("ok") is False:
        here = [PT_NAMES.get(p, p) for p in data.get("types_here") or []]
        raise ToolFailure(
            f"Too few sales of {PT_NAMES.get(property_type, property_type)} in {outcode.upper()} to give "
            f"medians. " + (f"Types with figures there: {', '.join(here)}." if here else
                            "No figures for any type there: check it is a postcode district such as 'BS7'."))
    data["url"] = _trends_url(outcode, property_type, factor)
    return _ok(**data, sources=[SRC_HMLR, SRC_EPC])


NearbyKind = Literal["rail_station", "tram_metro", "bus_stop", "ferry", "school_primary",
                     "school_secondary", "cafe_restaurant", "pub_bar", "takeaway", "supermarket",
                     "park", "playground"]


NEARBY_BATCH_MAX = 50


@mcp.tool(title="What is nearby", annotations=READ)
@logged("whats_nearby")
async def whats_nearby(
    akey: Annotated[AKey | None, Field(description="Address key of one home, from search_addresses. Give this or `akeys`.")] = None,
    radius_m: Annotated[int, Field(ge=100, le=2000, description="Radius in metres, 100 to 2000.")] = 800,
    closest: Annotated[NearbyKind | None, Field(description="Instead of counts within the radius, list the N closest places of this one kind (expanding rings up to 16 km).")] = None,
    n: Annotated[int, Field(ge=1, le=25, description="How many closest places, with `closest`: 10 by default, up to 25 for one home; at most 3 per home with `akeys`.")] = 10,
    akeys: Annotated[list[AKey] | None, Field(
        min_length=1, max_length=NEARBY_BATCH_MAX,
        description="Several homes in one call, up to 50 address keys: use this instead of calling "
                    "once per home, for example for every home recent_sales or search_homes_for_sale returned.")] = None,
    ctx: Context = None,
) -> dict:
    """Train, tram and ferry stops, bus stops, primary and secondary schools, cafes and
    restaurants, pubs, takeaways, supermarkets, parks and play areas around a home, from
    official open data, within 100 m to 2 km: for each kind the count, the nearest one and
    its distance. Pass `closest` with a kind to get the nearest N of that kind instead,
    however far (the way to ask "how far is the station" for a rural home). To compare
    many homes, pass up to 50 keys in `akeys` and get one row per home in `homes`: one
    call covers the whole list.

    Use it for amenities and transport. Crime and air quality are in address_report;
    compare_homes also names the nearest station, school, supermarket and park for two to
    four homes. Keys come from search_addresses or any row another tool returned. A kind
    with nothing inside the radius is left out of `kinds` (an empty `kinds` means nothing
    within it at all): widen `radius_m` or use `closest`. Schools here are by distance only, not catchment
    or Ofsted rating."""
    if bool(akey) == bool(akeys):
        raise ToolFailure("Give either `akey` (one home) or `akeys` (up to 50 homes), not both and not neither.")
    if akeys:
        keys = list(dict.fromkeys(akeys))
        params = {"akeys": ",".join(keys), "r": radius_m}
        if closest:
            params.update({"closest": closest, "n": min(n, 3)})
        data = await _get("/api/nearby", params, ctx)
        for h in data.get("homes") or []:
            h["url"] = _nearby_url(h["akey"], radius_m)
        data["url"] = f"{PUBLIC}/tools/whats-nearby"
        return _ok(**data, sources=[SRC_POI, SRC_OS])
    params = {"akey": akey, "r": radius_m}
    if closest:
        params.update({"closest": closest, "n": n})
    data = await _get("/api/nearby", params, ctx)
    data["url"] = _nearby_url(akey, radius_m)
    return _ok(**data, sources=[SRC_POI, SRC_OS])


@mcp.tool(title="Freehold or leasehold check", annotations=READ)
@logged("tenure_check")
async def tenure_check(
    query: Annotated[str, Field(min_length=3, max_length=160, description="Address or street with postcode, e.g. '12 Church Lane, BS7 8AB'.")],
    ctx: Context = None,
) -> dict:
    """Whether homes matching an address are freehold or leasehold, from the tenure recorded
    on the latest Land Registry sale of each exact address, with that sale's date. Up to 12
    candidates; null tenure means no recorded sale with a tenure. Matching is by text: when
    `fuzzy` is true or the rows are not the address asked for, use search_addresses, whose
    rows carry `tenure_at_last_sale` for the exact home."""
    data = await _get("/api/tenure", {"q": query}, ctx)
    if data.get("need_query"):
        raise ToolFailure("Give at least three characters of an address.")
    data["url"] = f"{PUBLIC}/tools/freehold-check?q={_q(query)}"
    return _ok(**data, sources=[SRC_HMLR])


@mcp.tool(title="When was a house built", annotations=READ)
@logged("house_age")
async def house_age(
    query: Annotated[str, Field(min_length=3, max_length=160, description="Address or street with postcode.")],
    ctx: Context = None,
) -> dict:
    """The construction age band of homes matching an address, from the Energy Performance
    Certificate, with what that era means for the construction, plus the first recorded sale
    year as corroboration and the property type. Up to 12 candidates.

    Use it for "when was this house built" and "what is it built like" (solid walls, cavity
    walls, asbestos era). It takes the address as text, so no `akey` is needed; for the
    full energy picture of one home use energy_performance. Matching is by text: when
    `fuzzy` is true the rows are near matches, not the address asked for, and
    search_addresses finds the exact home. `band` null means that home has no certificate
    with an age band; `first_yr` is the first sale since 1995, not the build year."""
    data = await _get("/api/built", {"q": query}, ctx)
    if data.get("need_query"):
        raise ToolFailure("Give at least three characters of an address.")
    data["url"] = f"{PUBLIC}/tools/when-was-my-house-built?q={_q(query)}"
    return _ok(**data, sources=[SRC_EPC, SRC_HMLR])


# ---------------------------------------------------------------- resources
STUDIES = {
    "leasehold-freehold-statistics": {
        "title": "Leasehold and freehold statistics by district",
        "csvs": ["leasehold-freehold-statistics.csv"],
        "blurb": "Share of homes that are leasehold or freehold, by local authority district and property type, from 15.7 million Land Registry records.",
    },
    "price-per-square-metre": {
        "title": "House prices per square metre in England and Wales",
        "csvs": ["price-per-square-metre.csv", "price-per-square-metre-by-area.csv"],
        "blurb": "Median pounds per square metre by postcode district, postcode area and property type, from Land Registry sales matched to EPC floor areas.",
    },
    "how-long-people-stay-in-their-homes": {
        "title": "How long people stay in their homes",
        "csvs": ["how-long-people-stay-in-their-homes.csv", "how-long-people-stay-in-their-homes-cohorts.csv",
                 "how-long-people-stay-in-their-homes-flips.csv"],
        "blurb": "Median gap between consecutive sales of the same home by district and type, the share not sold for 20 years or more, and the share resold within two years.",
    },
    "how-big-are-homes-in-england-and-wales": {
        "title": "How big are homes in England and Wales",
        "csvs": ["how-big-are-homes-in-england-and-wales.csv", "how-big-are-homes-in-england-and-wales-built-form.csv"],
        "blurb": "Median total floor area by district, property type and built form, from 20 million energy performance certificates.",
    },
}


@mcp.resource("getmovely://research", mime_type="application/json", title="Research studies index")
def research_index() -> dict:
    """The four GetMovely research studies with their page URLs and CSV resources."""
    return {"studies": [{"slug": s, "title": v["title"], "summary": v["blurb"],
                         "page": f"{PUBLIC}/research/{s}",
                         "csv_resources": [f"getmovely://research/{c}" for c in v["csvs"]]}
                        for s, v in STUDIES.items()],
            "licence": [SRC_HMLR, SRC_EPC],
            "attribute_as": "GetMovely (2026), <study title>, getmovely.co.uk/research"}


@mcp.resource("getmovely://research/{filename}", mime_type="text/csv", title="Research study CSV")
async def research_csv(filename: str) -> str:
    """One research study's data table as CSV, exactly as published on getmovely.co.uk
    (method and limitations on the study page). Filenames are listed in getmovely://research."""
    allowed = {c for v in STUDIES.values() for c in v["csvs"]}
    if filename not in allowed:
        raise ValueError(f"unknown study file; one of {sorted(allowed)}")
    return await _get_text(f"/research/{filename}")


@mcp.resource("getmovely://about", mime_type="text/markdown", title="About GetMovely (llms.txt)")
async def about() -> str:
    """What GetMovely is, what it offers, where the data comes from, and every public page,
    in the llms.txt format."""
    return await _get_text("/llms.txt")


# ---------------------------------------------------------------- human page and health
_TOOLS_CACHE: list[dict] | None = None
_PROMPTS_CACHE: list[dict] | None = None

# How the page groups the tools. A tool missing from this map still appears,
# under "More", so adding a tool can never leave it off the page.
TOOL_GROUPS = [
    ("Search like a buyer", ["search_homes_for_sale", "find_homes_not_for_sale", "recent_sales",
                             "stamp_duty", "draft_letter"]),
    ("Find a home", ["search_addresses", "find_address_from_sale_history", "get_address"]),
    ("One home", ["address_report", "sale_history", "price_check", "negotiation_brief", "buyer_checks",
                  "energy_performance", "compare_homes",
                  "whats_nearby", "tenure_check", "house_age", "street_price_per_m2"]),
    ("An area", ["area_overview", "compare_areas", "price_trends", "district_statistics"]),
    ("Guides", ["search_guides", "read_guide"]),
]


async def _tool_summaries() -> list[dict]:
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        tools = await mcp.list_tools()
        _TOOLS_CACHE = [{"name": t.name, "title": t.title or t.name,
                         "description": (t.description or "").strip()} for t in tools]
    return _TOOLS_CACHE


async def _prompt_summaries() -> list[dict]:
    global _PROMPTS_CACHE
    if _PROMPTS_CACHE is None:
        prompts = await mcp.list_prompts()
        _PROMPTS_CACHE = [{"name": p.name, "title": p.title or p.name,
                           "description": " ".join((p.description or "").split())} for p in prompts]
    return _PROMPTS_CACHE


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    return flat.split(". ")[0].rstrip(".") + "."


def _page(tools: list[dict], prompts: list[dict]) -> str:
    by_name = {t["name"]: t for t in tools}
    grouped = {n for _, names in TOOL_GROUPS for n in names}
    groups = TOOL_GROUPS + [("More", [t["name"] for t in tools if t["name"] not in grouped])]
    tool_html = "\n".join(
        f"<h3>{html.escape(title)}</h3>\n<table>" + "\n".join(
            f"<tr><td><code>{html.escape(n)}</code></td>"
            f"<td>{html.escape(_first_sentence(by_name[n]['description']))}</td></tr>"
            for n in names if n in by_name) + "</table>"
        for title, names in groups if any(n in by_name for n in names))
    prompt_html = "\n".join(
        f"<tr><td><code>{html.escape(p['name'])}</code></td><td><strong>{html.escape(p['title'])}.</strong> "
        f"{html.escape(_first_sentence(p['description']))}</td></tr>" for p in prompts)
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "SoftwareApplication",
        "name": "GetMovely MCP server", "url": MCP_URL, "applicationCategory": "DeveloperApplication",
        "operatingSystem": "Any", "softwareVersion": VERSION,
        "description": "Free, read-only Model Context Protocol server giving AI assistants a search of homes "
                       "for sale and homes not for sale, sold prices, tenure, energy certificates, price "
                       "checks, stamp duty, area data and guides for homes in England and Wales.",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "GBP"},
        "publisher": {"@type": "Organization", "name": "GetMovely", "url": PUBLIC},
    })
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UK sold house prices, Land Registry and EPC data MCP server, free, no key | GetMovely</title>
<meta name="description" content="Free UK property MCP server: HM Land Registry sold house prices, freehold or leasehold, EPC energy certificates, price checks and area comparisons for nearly 25 million homes in England and Wales. Read-only, no key, no account. Works with Claude, ChatGPT, Cursor and any MCP client.">
<link rel="canonical" href="{MCP_URL}">
<link rel="icon" href="{PUBLIC}/favicon.svg">
<meta property="og:title" content="UK sold house prices, Land Registry and EPC data MCP server, free, no key | GetMovely">
<meta property="og:description" content="Free, read-only MCP server: sold prices, tenure, EPC, price checks and area data for homes in England and Wales.">
<meta property="og:url" content="{MCP_URL}">
<meta property="og:image" content="{PUBLIC}/static/og.png">
<script type="application/ld+json">{ld}</script>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#1b1b1b}}
h1{{font-size:1.6rem}} h2{{font-size:1.15rem;margin-top:2rem}} h3{{font-size:1rem;margin:1.2rem 0 .3rem}}
code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.92em;background:#f4f2ee;border-radius:4px}}
code{{padding:.1em .3em}} pre{{padding:.8rem;overflow:auto}}
table{{border-collapse:collapse;width:100%}} td{{border-top:1px solid #e3e0da;padding:.45rem .3rem;vertical-align:top}}
td:first-child{{white-space:nowrap;padding-right:.9rem}}
.muted{{color:#5b5b5b}} a{{color:#8a3b2b}}
</style></head><body>
<p><a href="{PUBLIC}/">GetMovely</a> &rsaquo; For AI assistants</p>
<h1>UK sold house prices and property data for AI assistants: the GetMovely MCP server</h1>
<p>GetMovely runs a free <abbr title="Model Context Protocol">MCP</abbr> server at
<code>{MCP_URL}</code>. Connect it and your assistant can answer questions about any home in
England and Wales from the public record: whether it is owner-occupied, whether it is listed
for sale, what it sold for and when, freehold or leasehold, its energy certificate, whether
a price is in line with the district and the street, what is nearby, and how postcode
districts compare. It can search like a buyer: homes for sale that fit a budget and a size,
homes for sale described in your own words ("a riverside flat with a balcony in London"),
homes that are not for sale but fit what you want (so you can write to their owners), what
has sold nearby, and how much stamp duty a price means. It can also read our guides on
buying, selling and writing to an owner, and when you have agreed a letter with your
assistant it hands you a link that opens that letter on GetMovely, ready for you to read
and send. No account, no key.
<span class="muted">Version {VERSION}, {len(tools)} tools.</span></p>

<h2>Connect</h2>
<table>
<tr><td><strong>Claude</strong> (claude.ai, desktop)</td><td>Settings &rsaquo; Connectors &rsaquo; Add custom connector &rsaquo; URL <code>{MCP_URL}</code>. No sign-in step.</td></tr>
<tr><td><strong>Claude Code</strong></td><td><code>claude mcp add --transport http getmovely {MCP_URL}</code></td></tr>
<tr><td><strong>ChatGPT</strong></td><td>Open <code>chatgpt.com/plugins</code>, press the plus button, choose "Add custom MCP server" and give the URL above. Whether the option appears depends on your plan and workspace settings.</td></tr>
<tr><td><strong>Cursor, VS Code, Windsurf</strong></td><td>Add a remote MCP server of type <code>http</code> with the URL above.</td></tr>
<tr><td><strong>OpenClaw</strong></td><td><code>openclaw mcp add getmovely --url {MCP_URL} --transport streamable-http</code></td></tr>
<tr><td><strong>Anything else</strong></td><td>Streamable HTTP at the URL above, no authentication. Registry name <code>uk.co.getmovely/getmovely</code>. Source code: <a href="https://github.com/PlalalaDa/getmovely-mcp">github.com/PlalalaDa/getmovely-mcp</a>. Listing and score: <a href="https://glama.ai/mcp/connectors/uk.co.getmovely/getmovely">Glama</a>.</td></tr>
</table>

<h2>Owner-occupied, for sale, and response likelihood</h2>
<p>For every home the server says two things from public records, and what they add up to:</p>
<table>
<tr><td><strong>Owner-occupied</strong></td><td>The home's latest Energy Performance Certificate records it as owner-occupied. This is public information from the national register.</td></tr>
<tr><td><strong>Listed for sale</strong></td><td>We have seen the home advertised for sale. The answer includes the asking price, the agent and a link to the advert.</td></tr>
<tr><td><strong>Response likelihood</strong></td><td>Listed for sale plus owner-occupied. <strong>High</strong>: both, so a letter reaches an owner who is selling. <strong>Medium</strong>: owner-occupied, not listed. <strong>Unknown</strong>: no public record that the owner lives there.</td></tr>
</table>
<p>Searches can be filtered by it, so your assistant can list the homes on a street or in a postcode district that are listed for sale and owner-occupied, and link you to the same list on GetMovely.</p>

<h2>Try asking</h2>
<ul>
<li>"Find me a 3-bed house under &pound;400,000 in BS7."</li>
<li>"A riverside flat with a balcony in London, under &pound;600,000."</li>
<li>"A chain-free cottage that needs modernising near Totnes."</li>
<li>"Which houses on Elm Road, BS7 are not for sale but owner-occupied? I'd like to write to them."</li>
<li>"What has sold in SW19 this year for under &pound;500,000?"</li>
<li>"How much stamp duty on &pound;425,000 as a first-time buyer? And in Wales?"</li>
<li>"I'm viewing Flat 5, 41 Denning Road NW3 on Saturday. What should I know?"</li>
<li>"Is 143A Kingston Road SW19 for sale, and does the owner live there?"</li>
<li>"Which homes on Water Lane, Totton are owner-occupied and for sale right now?"</li>
<li>"Is &pound;1m a fair price for it?"</li>
<li>"The listing hides the house number but says it last sold for &pound;450,000 in 2019, on Merton Road SW19. Which home is it?"</li>
<li>"Where could we afford a 100 square metre terraced house: BS7, SW19 or M20?"</li>
<li>"How long do people stay in their homes in Camden, and how much of it is leasehold?"</li>
<li>"Is it legal to write to a homeowner asking to buy their house?"</li>
</ul>

<h2>Tools</h2>
{tool_html}

<h2>Prompts</h2>
<p>Ready-made requests your client shows as slash commands or a prompt menu.</p>
<table>{prompt_html}</table>

<h2>Resources</h2>
<p>The four <a href="{PUBLIC}/research/price-per-square-metre">research studies</a> as CSV
(<code>getmovely://research</code>), every <a href="{PUBLIC}/guides">guide</a> as Markdown
(<code>getmovely://guides</code>) and a description of the site (<code>getmovely://about</code>).</p>

<h2>Limits</h2>
<ul>
<li>England and Wales only. Sold prices arrive two weeks to two months after a sale.</li>
<li>Fair use: about {MINUTE_BUDGET} requests a minute and {DAILY_BUDGET} a day per connection; address reports are 60 an hour, as on the website. A tool that compares several homes or districts uses one request for each.</li>
<li>Price checks and implied price ranges are arithmetic on public sale records, not valuations. Condition, lease length, floor level and plot are not in the data.</li>
<li>The stamp duty figure is an estimate from the rates published on GOV.UK and gov.wales for a standard purchase, not tax advice.</li>
<li>No tool sends anything or needs an account. A drafted letter is handed over as a link; you read it and send it on GetMovely yourself.</li>
<li>Every answer carries a <code>url</code> to the GetMovely page it came from. Please link it when you quote a figure.</li>
<li>Data: Contains HM Land Registry data &copy; Crown copyright and database right 2026, Open Government Licence v3.0; Energy Performance of Buildings Data under the same licence. Quote the <code>sources</code> lines when you republish a figure.</li>
</ul>
<h2>If something goes wrong</h2>
<ul>
<li><strong>"No exact match"</strong> from an address search: nothing matched what was typed, and the rows are the nearest names, which can be in another town. Check the spelling or add a postcode. Scotland and Northern Ireland are not covered, so an address there is never found.</li>
<li><strong>A list of <code>streets</code> instead of homes:</strong> the street name exists in several places. Say which town, or add the first half of the postcode.</li>
<li><strong>"address not found"</strong> or a refused address key: keys come from a search in the same conversation. Ask the assistant to search for the address again.</li>
<li><strong>"That place covers ... postcode districts":</strong> a city is too big for one search. Name a district such as BS7 or SW19; the message lists the ones in that place.</li>
<li><strong>"No recorded sale"</strong> or no energy rating: the home has not sold since 1995, or has no Energy Performance Certificate. That is a gap in the public record, not a fault.</li>
<li><strong>"more than {MINUTE_BUDGET} calls in a minute", "daily budget ... reached" or "rate limit ... reached":</strong> the fair-use limits above, counted per connection. Wait a minute (an hour for address reports, a day for the daily budget) and ask again.</li>
<li><strong>"GetMovely is not answering right now":</strong> try again in a minute; <a href="/mcp/health">the status page</a> says whether the server is up.</li>
<li><strong>The assistant cannot connect at all:</strong> check that the URL is exactly <code>{MCP_URL}</code>, that the transport is HTTP (Streamable HTTP), and that no sign-in or key was entered: there is none.</li>
</ul>
<p class="muted">GetMovely is independent and is not affiliated with Rightmove, Zoopla or OnTheMarket. Protocol 2026-07-28 and earlier. Problems or ideas: <a href="{PUBLIC}/contact">contact us</a>.
<a href="{PUBLIC}/privacy">Privacy</a> &middot; <a href="{PUBLIC}/terms">Terms</a> &middot; <a href="/mcp/health">Status</a></p>
</body></html>"""


@mcp.custom_route("/mcp/health", methods=["GET"])
async def health(request: Request) -> Response:
    ok = True
    try:
        r = await _http().get("/api/meta", headers={"User-Agent": f"GetMovely-MCP/{VERSION} health",
                                                   "X-GM-Probe": "mcp-health"}, timeout=5)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    return JSONResponse({"status": "ok" if ok else "degraded", "backend": "ok" if ok else "unreachable",
                         "version": VERSION, "tools": len(await _tool_summaries())},
                        status_code=200 if ok else 503,
                        headers={"Cache-Control": "no-store"})


@mcp.custom_route("/mcp/server.json", methods=["GET"])
async def server_json(request: Request) -> Response:
    """The registry entry, as published (draft until the listing exists)."""
    return JSONResponse({
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "uk.co.getmovely/getmovely",
        "title": "GetMovely",
        "description": "Search homes for sale and off-market, sold prices, EPC, stamp duty: 25m homes in England and Wales.",
        "websiteUrl": MCP_URL,
        "version": VERSION,
        "icons": [{"src": f"{PUBLIC}/static/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]}],
        "remotes": [{"type": "streamable-http", "url": MCP_URL}],
    })


# ---------------------------------------------------------------- 0.2.0 modules
# The tools, guides and prompts added in 0.2.0. Each module registers on THIS
# module object, passed in, so nothing ever imports gm_mcp a second time (see
# the module docstring). Order is the order they appear in tools/list.
def _load_extensions(core) -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gm_mcp_homes
    import gm_mcp_areas
    import gm_mcp_content
    import gm_mcp_search
    for mod in (gm_mcp_homes, gm_mcp_areas, gm_mcp_search, gm_mcp_content):
        mod.register(core)


_load_extensions(sys.modules[__name__])


class HumanPage:
    """Serve the HTML page to a browser on GET /mcp; pass everything else to
    the MCP transport. A browser sends Accept: text/html and no MCP headers; an
    MCP client's GET asks for text/event-stream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD") \
                and scope["path"].rstrip("/") == "/mcp":
            headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope["headers"]}
            accept = headers.get("accept", "")
            if "text/event-stream" not in accept and "mcp-protocol-version" not in headers \
                    and ("text/html" in accept or "*/*" in accept or not accept):
                # The same posture as the website's security_headers(): the
                # page has no script, so the CSP can say so.
                resp = HTMLResponse(_page(await _tool_summaries(), await _prompt_summaries()),
                                    headers={"Cache-Control": "public, max-age=600",
                                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                                        "img-src 'self' https://getmovely.co.uk; frame-ancestors 'none'",
                                             "X-Frame-Options": "DENY",
                                             "X-Content-Type-Options": "nosniff",
                                             "Referrer-Policy": "strict-origin-when-cross-origin"})
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


RPC_LOG_MAX_BODY = 256 * 1024


class RpcLog:
    """Log every JSON-RPC request on POST /mcp (except notifications) as an
    `ev: rpc` line in the call log, with the caller hash, user agent, probe
    marker, protocol version and, on `initialize` / `server/discover`, the
    client's own name and version (`clientInfo`). Tool calls are logged again by
    the tool itself with ok and ms; this layer is what records CONNECTIONS, which
    the Phase 1 gate counts (design notes, not published) and
    which the tool log alone could not see: a client that lists the tools and
    never calls one left no trace before this. The body is buffered (cap
    RPC_LOG_MAX_BODY, larger bodies pass through unparsed) and replayed to the
    transport unchanged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" \
                or scope["path"].rstrip("/") != "/mcp":
            await self.app(scope, receive, send)
            return
        chunks: list[dict] = []
        size = 0
        while True:
            msg = await receive()
            chunks.append(msg)
            size += len(msg.get("body", b""))
            if msg["type"] != "http.request" or not msg.get("more_body") or size > RPC_LOG_MAX_BODY:
                break
        if size <= RPC_LOG_MAX_BODY:
            try:
                self._log(scope, b"".join(c.get("body", b"") for c in chunks))
            except Exception as e:  # logging must never break a request
                log.warning("rpc log: %s", e)
        it = iter(chunks)

        async def replay():
            try:
                return next(it)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _log(scope, body: bytes) -> None:
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope["headers"]}
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        msgs = data if isinstance(data, list) else [data]
        ip = (headers.get("cf-connecting-ip") or headers.get("x-forwarded-for") or "").split(",")[0].strip() or "local"
        base = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ev": "rpc", "caller": _ip_hash(ip),
                "ua": headers.get("user-agent", "")[:160], "probe": _probe_value(headers.get("x-gm-probe", "")) or None,
                "country": _country_value(headers.get("cf-ipcountry", "")) or None}
        for m in msgs:
            if not isinstance(m, dict) or "method" not in m:
                continue
            method = str(m.get("method"))[:60]
            if method.startswith("notifications/"):
                continue
            params = m.get("params") if isinstance(m.get("params"), dict) else {}
            rec = dict(base, method=method,
                       proto=(params.get("protocolVersion") or headers.get("mcp-protocol-version") or None))
            # Legacy `initialize` carries clientInfo in params; 2026-07-28
            # `server/discover` carries it inside params._meta under a
            # namespaced key, so look there for any dict that names itself.
            info = params.get("clientInfo")
            if not isinstance(info, dict):
                meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                info = next((v for v in meta.values() if isinstance(v, dict) and v.get("name")), None)
            if isinstance(info, dict) and info.get("name"):
                rec["client"] = f"{info.get('name')}/{info.get('version') or '?'}"[:80]
            if method == "tools/call" and params.get("name"):
                rec["tool"] = str(params["name"])[:40]
            _write_log(rec)


SECURITY = TransportSecuritySettings(
    allowed_hosts=["getmovely.co.uk", "getmovely.co.uk:*", "www.getmovely.co.uk", "www.getmovely.co.uk:*",
                   "127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",],
    allowed_origins=[PUBLIC, "https://www.getmovely.co.uk", "http://127.0.0.1:*", "http://localhost:*"],
)

mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True,
                                  stateless_http=True, transport_security=SECURITY)


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield
    if _client is not None:
        await _client.aclose()


app = HumanPage(RpcLog(Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)))


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("GetMovely MCP %s on 127.0.0.1:%s, backend %s", VERSION, PORT, BACKEND)
    uvicorn.run(app, host="127.0.0.1", port=PORT, proxy_headers=True, forwarded_allow_ips="*",
                log_level="info", access_log=False)

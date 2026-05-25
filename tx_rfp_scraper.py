#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tx_rfp_scraper.py
=================
Refreshes the data file used by the TX Traffic RFP Console.

WHAT IT DOES
  1. Pulls the open Texas public-procurement feed (Texas SmartBuy / ESBD,
     read through the public Public Bid Tracker mirror).
  2. Keeps only solicitations whose text matches traffic-engineering
     keywords. The classifier now spans 11 categories: Traffic Signals,
     Signal Timing, ITS / Detection, Emerging Tech & AI, Traffic Management,
     Traffic Studies, Traffic Control, Signing & Marking, Active
     Transportation, Transportation Planning, and Traffic Engineering.
  3. Keeps only solicitations with a deadline inside the requested window.
  4. Maps Texas SmartBuy agency / member numbers to readable entity names
     and groups results by issuing entity (city / county / MPO / state).
  5. Writes rfp_data.json  -> load this in the HTML console ("Load data file").

HONEST SCOPE NOTE
  ESBD aggregates STATE agencies plus the subset of cities/counties that are
  Texas SmartBuy members. It is NOT a complete list of every Texas city/county
  RFP -- most municipalities post only on their own portals (BidNet, DemandStar,
  Bonfire, Ionwave, OpenGov, city sites), which have no shared API. For those,
  use the "Portal Directory" tab of the console, or a commercial aggregator
  (BidNet Direct Texas Purchasing Group / DemandStar). Extra source adapters
  can be added in the SOURCES section below.

USAGE
  python tx_rfp_scraper.py                 # default: 182-day window, ESBD only
  python tx_rfp_scraper.py --days 90       # custom window
  python tx_rfp_scraper.py --out data.json # custom output path
  python tx_rfp_scraper.py --from-file saved_page.html   # parse a saved ESBD dump
  python tx_rfp_scraper.py --insecure      # skip SSL verification (last resort)

  # Add a city's OWN portal (not on ESBD). One adapter per platform:
  python tx_rfp_scraper.py --no-esbd \\
      --portal-file "City of McAllen::city::McAllen_Procurement.html"
  # MANY cities at once -- list them in a text file, one per line:
  python tx_rfp_scraper.py --portal-list portals.txt \\
      --merge rfp_data.json --out rfp_data.json
  # Or drop saved pages in a folder (filename = jurisdiction name):
  python tx_rfp_scraper.py --portal-dir html \\
      --merge rfp_data.json --out rfp_data.json
  # Try live URLs through a headless browser (renders JavaScript portals):
  python tx_rfp_scraper.py --portal-list portals.txt --render \\
      --out rfp_data.json

HEADLESS RENDERING (--render)
  Live portal URLs normally return nothing useful: ProcureWare, Bonfire,
  Ionwave and DemandStar build their bid lists with JavaScript, which a
  plain HTTP fetch never runs. With --render the scraper loads each URL in
  a headless Chromium browser, lets the JavaScript run, and parses the
  finished page -- automatically, no manual saving. It is OPTIONAL:
      pip install playwright
      playwright install chromium
  Trade-offs: much slower (a browser per URL); a heavier dependency; and it
  still does NOT get past Cloudflare-style anti-bot blocks or fix dead URLs.
  Without --render (or without Playwright) live URLs use plain HTTP and the
  tool stays standard-library-only.
  # Merge a portal into an existing file without losing the ESBD records:
  python tx_rfp_scraper.py --no-esbd --merge rfp_data.json --out rfp_data.json \\
      --portal-file "City of McAllen::city::McAllen_Procurement.html"

PORTAL LIST FILE (--portal-list)
  A plain text file, one jurisdiction per line, same format as --portal-file:
      Name::type::path-or-url
  Lines beginning with # are comments. The path may be a saved .html file OR
  a live http(s) URL (the script fetches URLs directly -- no manual saving).

PORTAL ADAPTERS (cities not on ESBD)
  ProcureWare (e.g. mcallen.procureware.com) -- SUPPORTED. Its bid grid is in
  the page HTML, so a saved page or a live URL can be parsed.
  DemandStar -- NOT parseable from a saved page: it is a JavaScript app and the
  saved .html has no bid data. Use DemandStar's own alerts for those cities.

REQUIREMENTS
  Standard library only. NOTE for macOS: python.org builds do not trust the
  system keychain, so HTTPS can fail with 'CERTIFICATE_VERIFY_FAILED'. Fix it
  with `pip install certifi` -- this script then uses certifi automatically.
  --no-esbd + --portal-file with local files needs no network at all.
"""

import argparse
import datetime
import html
import json
import os
import re
import ssl
import sys
import urllib.request

# Optional: headless-browser rendering for JavaScript-built portals.
# The scraper runs fine without it (standard library only); --render simply
# becomes unavailable. To enable it, one-time:
#     pip install playwright
#     playwright install chromium
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _HAVE_PLAYWRIGHT = True
except ImportError:
    _HAVE_PLAYWRIGHT = False

# --------------------------------------------------------------------------
# SOURCES
# --------------------------------------------------------------------------
PBT_OPEN_BIDS = "https://publicbidtracker.com/texas/open-bids/"   # mirrors ESBD
ESBD_PORTAL   = "https://www.txsmartbuy.gov/esbd"                 # canonical record

# --------------------------------------------------------------------------
# Texas SmartBuy agency / member number -> entity name & type.
# Extend this map as you confirm more member numbers on the ESBD.
# type is one of: state | city | county | mpo | transit
# --------------------------------------------------------------------------
AGENCY_MAP = {
    "601":   ("Texas Department of Transportation (TxDOT)", "state"),
    "M0152": ("City of San Antonio",                        "city"),
    "M1612": ("Waco Metropolitan Planning Organization",    "mpo"),
    # --- add confirmed member numbers below, e.g.:
    # "M0xxx": ("City of ...", "city"),
    # "Cxxxx": ("... County",  "county"),
}

# --------------------------------------------------------------------------
# Traffic-engineering classification.
# Each (category, [keywords]) -- first matching category wins, so the order
# of this list matters. Move a category up to give it priority.
#
# MATCHING NOTE: keywords are matched as plain, case-insensitive SUBSTRINGS.
# That is why bare ambiguous words are deliberately NOT used on their own:
#   "ai"       would match maintain, repair, available, chair, campaign ...
#   "its"      would match units, limits, benefits, permits ...
#   "traffic"  would match human/drug trafficking ...
#   "planning" would match financial / capital / event planning ...
#   "signing"  would match designing / redesigning ...
# Instead we match the realistic multi-word phrases that actually appear in
# procurement titles ("artificial intelligence", "ai-powered", "intelligent
# transportation", "transportation planning", "roadway signing", etc.).
# Partial stems like "signal synchroniz" and "emerging technolog" are
# intentional -- they catch ...ation / ...ing / ...ies endings.
# --------------------------------------------------------------------------
CATEGORIES = [
    ("Traffic Signals", [
        "traffic signal", "signal controller", "signal design",
        "signal installation", "signal head", "signal cabinet",
        "signalization", "signal upgrade", "signal warrant",
        "signal interconnect", "signal pole", "mast arm",
        "pedestrian signal", "flashing beacon", "rectangular rapid",
        "rrfb", "battery backup", "school zone flasher",
        "signal maintenance", "signal rebuild"]),

    ("Signal Timing", [
        "signal timing", "signal retiming", "signal coordination",
        "signal synchroniz", "signal optimization", "adaptive signal",
        "traffic responsive", "timing plan", "coordination plan",
        "phasing plan", "split monitor"]),

    ("ITS / Detection", [
        "intelligent transportation", "its master plan", "its deployment",
        "its architecture", "autoscope", "vehicle detection",
        "video detection", "radar detection", "bluetooth detection",
        "wavetronix", "connected vehicle", "v2x", "v2i",
        "fiber optic", "fiber communication", "traffic management center",
        "advanced traffic management", "dynamic message sign",
        "changeable message sign", "traffic camera", "ramp meter",
        "travel time system", "transit signal priority", "tsmo",
        "emergency vehicle preemption", "detection system",
        "traffic monitoring system"]),

    ("Emerging Tech & AI", [
        "artificial intelligence", "machine learning", "ai-powered",
        "ai-driven", "ai-based", "ai-enabled", "ai system", "ai solution",
        "ai/ml", "a.i.", "emerging technolog", "emerging mobility",
        "smart city", "smart mobility", "smart corridor",
        "smart intersection", "digital twin", "predictive analytics",
        "computer vision", "automated vehicle", "autonomous vehicle",
        "automated shuttle", "lidar", "data analytics platform"]),

    ("Traffic Management", [
        "traffic management", "congestion management", "incident management",
        "traffic operations", "transportation management",
        "transportation operations", "freeway management",
        "arterial management", "transportation systems management"]),

    ("Traffic Studies", [
        "traffic study", "traffic impact", "traffic count",
        "traffic analysis", "origin-destination", "origin destination",
        "travel demand", "speed study", "traffic data collection",
        "intersection analysis", "level of service", "warrant study",
        "turning movement"]),

    ("Traffic Control", [
        "traffic control", "work zone", "workzone", "traffic calming",
        "roundabout", "traffic control device", "lane closure",
        "maintenance of traffic"]),

    ("Signing & Marking", [
        "pavement marking", "roadway signing", "highway signing",
        "sign installation", "sign replacement", "signage", "striping",
        "wayfinding", "sign inventory", "guide sign", "regulatory sign",
        "delineation", "raised pavement marker"]),

    ("Active Transportation", [
        "active transportation", "complete street", "bicycle plan",
        "pedestrian plan", "bike lane", "bicycle lane", "shared use path",
        "shared-use path", "multi-use path", "sidewalk",
        "hike and bike", "trail design", "trail plan", "regional trail",
        "safe routes to school", "vision zero", "micromobility",
        "ada transition", "pedestrian safety", "bicycle safety",
        "crosswalk"]),

    ("Transportation Planning", [
        "transportation planning", "transportation plan", "mobility plan",
        "mobility study", "corridor study", "corridor plan",
        "thoroughfare plan", "thoroughfare", "master mobility",
        "accessibility and mobility", "feasibility study",
        "subregional planning", "sub-regional planning",
        "sub regional planning", "regional planning", "regional mobility",
        "long-range transportation plan", "long range transportation plan",
        "metropolitan transportation plan", "unified planning work program",
        "transit study", "small area plan",
        "land use and transportation", "transportation impact analysis"]),

    ("Traffic Engineering", [
        "traffic engineering", "transportation engineering",
        "transportation consultant", "traffic consultant",
        "on-call engineering", "on-call traffic",
        "general engineering services"]),
]
ALL_KEYWORDS = [kw for _, kws in CATEGORIES for kw in kws]


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------
def make_ssl_context(insecure=False):
    """Return an SSL context with a CA bundle that actually works.

    macOS python.org builds do NOT use the system keychain, so they raise
    'CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate'.
    Using certifi's bundle fixes that. insecure=True skips verification
    entirely -- a last resort, e.g. behind a corporate TLS-inspecting proxy.
    """
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # no certifi -> default context (works fine on most non-macOS setups)
        return ssl.create_default_context()


def fetch(url, insecure=False, extra_headers=None):
    """GET a URL and return decoded text. Raises on failure.

    Uses a normal browser User-Agent: some government sites reject the
    default urllib agent outright. This does NOT bypass real anti-bot
    systems (Cloudflare challenges, etc.) -- those still return 403.
    extra_headers, if given, are merged in (e.g. Origin/Referer for an API).
    """
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    ctx = make_ssl_context(insecure)
    with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_rendered(url, insecure=False, timeout=45):
    """Fetch a URL through a headless Chromium browser, returning the HTML
    AFTER JavaScript has run -- so JS-built bid grids (ProcureWare, Bonfire,
    Ionwave, DemandStar) are present, the way a 'Save Page As' captures them.

    Requires Playwright (`pip install playwright` + `playwright install
    chromium`). Slower than fetch() -- it launches a browser per call --
    and it still does NOT defeat Cloudflare-style anti-bot challenges.
    """
    if not _HAVE_PLAYWRIGHT:
        raise RuntimeError("Playwright is not installed")
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0.0.0 Safari/537.36")
    with _sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=ua,
                                          ignore_https_errors=insecure)
            page = context.new_page()
            try:
                # networkidle waits for AJAX grids; it can itself time out on
                # pages that poll forever -- if so, take whatever has loaded.
                page.goto(url, wait_until="networkidle",
                          timeout=timeout * 1000)
            except Exception:                          # noqa: BLE001
                pass
            page.wait_for_timeout(2000)                # let late renders settle
            return page.content()
        finally:
            browser.close()


# --------------------------------------------------------------------------
# Parsing  --  Public Bid Tracker open-bids table
# Each bid is rendered as a summary <tr> followed by a detail <tr>.
# We pull: solicitation #, organization code, description, deadline date.
# --------------------------------------------------------------------------
ROW_RE = re.compile(
    r"Bid\s*/\s*Solicitation\s*#\s*(?P<bid>.+?)\s+"
    r"Issuing\s*Agency\s*(?P<org>[A-Za-z0-9]+)\s+"
    r"Full\s*Description\s*(?P<desc>.+?)\s+"
    r"Deadline\s*.*?(?P<date>\d{4}-\d{2}-\d{2})",
    re.S)


def parse_rows(text):
    """Return list of dicts: {bid, org, desc, deadline} from page text."""
    text = html.unescape(text)
    # collapse tags/whitespace so the detail blocks become flat text
    flat = re.sub(r"<[^>]+>", " ", text)
    flat = re.sub(r"\s+", " ", flat)
    out, seen = [], set()
    for m in ROW_RE.finditer(flat):
        bid = m.group("bid").strip(" #")
        if bid in seen:
            continue
        seen.add(bid)
        out.append({
            "bid":      bid,
            "org":      m.group("org").strip(),
            "desc":     m.group("desc").strip(),
            "deadline": m.group("date").strip(),
        })
    return out


# --------------------------------------------------------------------------
# Classification & filtering
# --------------------------------------------------------------------------
def classify(desc):
    """Return category name if the description is traffic-related, else None."""
    low = (desc or "").lower()
    for category, kws in CATEGORIES:
        if any(kw in low for kw in kws):
            return category
    return None


def within_window(deadline_str, days):
    """True if deadline is today..today+days (still open & inside window)."""
    try:
        d = datetime.date.fromisoformat(deadline_str)
    except ValueError:
        return False
    today = datetime.date.today()
    return today <= d <= today + datetime.timedelta(days=days)


def resolve_entity(org_code):
    """Map an org code to (name, type). Unknown codes pass through."""
    if org_code in AGENCY_MAP:
        return AGENCY_MAP[org_code]
    # heuristic fallback by code prefix
    if org_code.startswith("C"):
        return ("Texas SmartBuy member %s (county)" % org_code, "county")
    if org_code.startswith("M"):
        return ("Texas SmartBuy member %s (local government)" % org_code, "city")
    return ("State agency %s" % org_code, "state")


# --------------------------------------------------------------------------
# PORTAL ADAPTERS
# --------------------------------------------------------------------------
# City/county bids that are NOT on ESBD live on the city's own eProcurement
# platform. Each platform needs one adapter; an adapter then works for every
# city on that platform. Add new platforms here as you confirm their markup.
#
#   ProcureWare  -> server-renders a Kendo grid; the bid rows ARE in the HTML,
#                   so a saved page (or a live fetch) can be parsed.  [SUPPORTED]
#   DemandStar   -> a JavaScript single-page app; the saved HTML is an empty
#                   shell with no bid data. Cannot be parsed from saved HTML.
# --------------------------------------------------------------------------
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _us_date_to_iso(s):
    """'5/26/2026 4:00 PM' -> '2026-05-26'.  '' if unparseable."""
    m = _DATE_RE.search(s or "")
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)), int(m.group(2)))


def detect_platform(text, url=""):
    """Identify which eProcurement platform a page (or URL) belongs to.

    Strong STRUCTURAL markers (the actual bid module of a platform) are
    checked first, so a page that merely *links* to another platform is
    not misclassified -- e.g. a CivicPlus page that links out to Bonfire
    must still be read as CivicPlus.
    """
    low = text.lower()
    u = (url or "").lower()
    # --- strong structural markers: this page IS that platform's bid page
    if "biditems" in low or "listitemsrow" in low or "bidsheader" in low:
        return "civicplus"
    if "bfconstants" in low or "projectnameid" in low or "bonfirehub.com" in u:
        return "bonfire"
    if "fullbidview_" in low:
        return "procureware"
    if "sol-table mets-table" in low:
        return "bidnet"
    if "procurement.opengov.com" in low:
        return "opengov"
    # --- weaker brand-name fallbacks
    if "procureware" in low or "procureware.com" in u:
        return "procureware"
    if "demandstar" in low or "demandstar.com" in u:
        return "demandstar"
    if "ionwave" in low or "ionwave.net" in u:
        return "ionwave"
    if "publicpurchase" in low or "publicpurchase.com" in u:
        return "publicpurchase"
    if "bidnetdirect" in u or "bidnet direct" in low:
        return "bidnet"
    if "civicplus" in low or u.endswith("/bids.aspx"):
        return "civicplus"
    if "napc.pro" in low or "texasbids" in low or "bids.net" in u:
        return "napc"
    return "unknown"


def parse_procureware(page_text):
    """Parse a ProcureWare /Bids grid into [{bid,title,status,due,url}].

    Columns are mapped by their stable data-field names (FullBidView_*), not
    by position, so the adapter survives a city hiding/reordering columns.
    """
    fields = re.findall(r'<th[^>]*data-field="(FullBidView_[^"]+)"', page_text)
    if not fields:
        return []
    idx = {f: k for k, f in enumerate(fields)}

    def col(short):
        return idx.get("FullBidView_" + short)

    ci_num, ci_ttl = col("Number"), col("Title")
    ci_stat, ci_due = col("CalculatedAdjustedStatus"), col("DueDate")

    low = page_text.lower()
    i, j = low.find("<tbody"), low.find("</tbody>")
    if i == -1 or j == -1:
        return []
    rows = re.findall(r"<tr [^>]*data-uid=[^>]*>(.*?)</tr>", page_text[i:j], re.S)

    out = []
    for r in rows:
        tds = re.findall(r"<td\b[^>]*>(.*?)</td>", r, re.S)
        if ci_num is None or ci_num >= len(tds):
            continue

        def cell(k):
            if k is None or k >= len(tds):
                return ""
            return html.unescape(re.sub(r"<[^>]+>", " ", tds[k])).strip()

        link = re.search(r'href="([^"]*?/Bids/[0-9A-Fa-f\-]{36})"', r)
        out.append({
            "bid":    cell(ci_num),
            "title":  cell(ci_ttl),
            "status": cell(ci_stat),
            "due":    _us_date_to_iso(cell(ci_due)),
            "url":    link.group(1) if link else "",
        })
    return out


def build_from_procureware(days, page_text, entity_name, entity_type,
                           keep_all=False):
    """Turn a ProcureWare page into console records (classified & filtered)."""
    base = re.search(r"https://[a-z0-9.\-]+\.procureware\.com", page_text.lower())
    portal = (base.group(0) + "/Bids") if base else ""
    records = []
    for b in parse_procureware(page_text):
        if b["status"] and "open" not in b["status"].lower():
            continue                                   # skip closed/awarded
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue                                   # closed or out of window
        records.append({
            "bid":        b["bid"],
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "ProcureWare portal",
            "url":        b["url"] or portal,
        })
    return records


def parse_napc(page_text):
    """Parse a NAPC '<state>bids.net' aggregator page (e.g. texasbids.net).

    Layout: each bid is a <tr> of three <td> -- date | <a>title</a> |
    location -- immediately followed by a <tr> with a 'Scope:' cell.
    """
    low = page_text.lower()
    i, j = low.find("<table"), low.find("</table>")
    if i == -1 or j == -1:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page_text[i:j], re.S)
    out, pending = [], None
    for r in rows:
        if pending is not None and "scope:" in r.lower():
            desc = re.sub(r"\s+", " ",
                          html.unescape(re.sub(r"<[^>]+>", " ", r))).strip()
            pending["desc"] = re.sub(r"(?i)^scope:\s*", "", desc)
            out.append(pending)
            pending = None
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(tds) < 2:
            continue
        datetxt = re.sub(r"<[^>]+>", "", tds[0]).strip()
        dm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", datetxt)
        if not dm:
            continue
        if pending is not None:           # previous bid had no scope row
            out.append(pending)
        link = re.search(r'href="([^"]+)"', tds[1])
        title = re.sub(r"\s+", " ",
                       html.unescape(re.sub(r"<[^>]+>", " ", tds[1]))).strip()
        yr = dm.group(3)
        yr = ("20" + yr) if len(yr) == 2 else yr
        url = link.group(1) if link else ""
        idm = re.search(r"/(\d{6,})-", url)
        pending = {
            "bid":   idm.group(1) if idm else "",
            "title": title,
            "due":   "%04d-%02d-%02d" % (int(yr), int(dm.group(1)),
                                         int(dm.group(2))),
            "url":   url,
            "desc":  "",
        }
    if pending is not None:
        out.append(pending)
    return out


def build_from_napc(days, page_text, entity_name, entity_type, keep_all=False):
    """Turn a NAPC aggregator page into classified, in-window records."""
    records = []
    for b in parse_napc(page_text):
        category = classify((b["title"] + " " + b["desc"]).strip())
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue                      # past-due / stale aggregator entry
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "texasbids.net aggregator",
            "url":        b["url"],
        })
    return records


def parse_bonfire(page_text):
    """Parse a Bonfire (bonfirehub.com) opportunities table from a SAVED page.

    Bonfire is a JavaScript app -- the LIVE page has no rows. A page saved
    AFTER the opportunity list has rendered DOES contain them. Real data
    rows carry a 'data-order=<unix-timestamp>' close-date attribute that the
    JS template rows do not, which is how we tell them apart.
    """
    out = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page_text, re.S):
        dm = re.search(r'data-order="(\d{9,})"', r)
        if not dm:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        if len(tds) < 3:
            continue

        def txt(k):
            return re.sub(r"\s+", " ",
                          html.unescape(re.sub(r"<[^>]+>", " ", tds[k]))).strip()

        try:
            due = datetime.datetime.fromtimestamp(
                int(dm.group(1)), datetime.timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            due = ""
        link = re.search(r'href="([^"]*?/opportunities/[^"]+)"', r)
        out.append({
            "bid":    txt(1),
            "title":  txt(2),
            "status": txt(0),
            "due":    due,
            "url":    link.group(1) if link else "",
        })
    return out


def build_from_bonfire(days, page_text, entity_name, entity_type,
                       keep_all=False):
    """Turn a Bonfire opportunities page into classified, in-window records."""
    records = []
    for b in parse_bonfire(page_text):
        if b["status"] and "open" not in b["status"].lower():
            continue
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "Bonfire portal",
            "url":        b["url"],
        })
    return records


def parse_civicplus(page_text):
    """Parse a CivicPlus 'Bids.aspx' bid module (server-rendered).

    The module is a <div class="bidItems listItems"> holding one
    <div class="listItemsRow bid"> per bid. Each has a title <a>, a
    'Bid No.' span, and a status block with 'Open/Closed' + a close date.
    """
    low = page_text.lower()
    i = low.find("biditems")
    if i == -1:
        return []
    j = low.find("submitbidform", i)
    block = page_text[i: j if j != -1 else i + 40000]
    out = []
    for seg in re.split(r'class="listItemsRow[^"]*bid[^"]*"', block)[1:]:
        am = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        if not am:
            continue
        title = re.sub(r"\s+", " ",
                       html.unescape(re.sub(r"<[^>]+>", "", am.group(2)))).strip()
        if not title:
            continue
        bm = re.search(r"Bid\s*No\.?\s*</strong>\s*([^<]+)", seg, re.I)
        sm = re.search(r">\s*(Open|Closed|Awarded|Cancelled|Pending)\s*<",
                       seg, re.I)
        dm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", seg)
        out.append({
            "bid":    html.unescape(bm.group(1)).strip() if bm else "",
            "title":  title,
            "status": sm.group(1) if sm else "",
            "due":    ("%04d-%02d-%02d" % (int(dm.group(3)), int(dm.group(1)),
                                           int(dm.group(2)))) if dm else "",
            "url":    am.group(1),
        })
    return out


def build_from_civicplus(days, page_text, entity_name, entity_type,
                         keep_all=False):
    """Turn a CivicPlus bid module into classified, in-window records."""
    records = []
    for b in parse_civicplus(page_text):
        if b["status"] and "open" not in b["status"].lower():
            continue
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "CivicPlus portal",
            "url":        b["url"],
        })
    return records


def _mdy_to_iso(s):
    """Convert an American 'M/D/YYYY' (optionally followed by a time) into
    an ISO 'YYYY-MM-DD' string. Returns '' if it cannot be parsed."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    if not m:
        return ""
    return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(1)),
                               int(m.group(2)))


# ----- Ionwave (Telerik RadGrid 'SourcingEvents' page) ----------------------
def parse_ionwave(page_text):
    """Parse an Ionwave SourcingEvents bid grid.

    Ionwave is ASP.NET WebForms; the bid list is a Telerik RadGrid whose
    data rows carry class rgRow / rgAltRow. Columns are:
      [icon] Bid Number | Bid Title | Bid Type | Organization
             | Bid Issue Date | Bid Close Date/Time
    The grid is server-rendered, so a saved page (or a --render fetch)
    contains the rows.
    """
    out = []
    rows = re.findall(
        r'<tr[^>]*class="[^"]*rg(?:Row|AltRow)[^"]*"[^>]*>(.*?)</tr>',
        page_text, re.S)
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
        vals = [re.sub(r"\s+", " ",
                       html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                for c in cells]
        if len(vals) < 7 or not vals[2]:
            continue
        link = re.search(r'href="([^"]+)"', r)
        out.append({
            "bid":   vals[1],
            "title": vals[2],
            "due":   _mdy_to_iso(vals[6]),
            "url":   link.group(1) if link else "",
        })
    return out


def build_from_ionwave(days, page_text, entity_name, entity_type,
                       source_url="", keep_all=False):
    """Turn an Ionwave SourcingEvents grid into classified, in-window records."""
    records = []
    for b in parse_ionwave(page_text):
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "Ionwave portal",
            "url":        b["url"] or source_url,
        })
    return records


# ----- DemandStar (public agency/search JSON API) ---------------------------
DEMANDSTAR_API = "https://api.demandstar.com/contents/agency/search?id=%s"


def demandstar_guid(url):
    """Pull the agency GUID (a standard UUID) out of a DemandStar URL."""
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", url or "")
    return m.group(0) if m else ""


def fetch_demandstar(guid, insecure=False):
    """Fetch and parse a DemandStar agency's bid list from its public API.

    This is the same unauthenticated endpoint DemandStar's own public
    agency page uses to display its bids -- no login or API key, it just
    needs ordinary browser Origin/Referer headers.
    """
    text = fetch(DEMANDSTAR_API % guid, insecure=insecure, extra_headers={
        "Accept":  "application/json",
        "Origin":  "https://www.demandstar.com",
        "Referer": "https://www.demandstar.com/",
    })
    return json.loads(text)


def parse_demandstar_bids(data):
    """Extract bid dicts from a DemandStar agency/search JSON response."""
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        return []
    out = []
    for b in result:
        if not isinstance(b, dict):
            continue
        bid_id = b.get("bidId")
        out.append({
            "bid":    str(b.get("bidIdentifier") or bid_id or "").strip(),
            "title":  (b.get("bidName") or "").strip(),
            "status": (b.get("status") or "").strip(),
            "due":    _mdy_to_iso(b.get("dueDate", "")),
            "url":    ("https://www.demandstar.com/app/limited/bids/%s/details"
                       % bid_id) if bid_id else "",
        })
    return out


def extract_demandstar_from_har(har_text):
    """Pull the agency/search JSON response out of a saved DemandStar HAR.

    A HAR is a browser network capture; the agency/search entry holds the
    exact JSON the browser received, so this works fully offline.
    """
    try:
        har = json.loads(har_text)
    except ValueError:
        return None
    for e in har.get("log", {}).get("entries", []):
        url = e.get("request", {}).get("url", "")
        if "agency/search" in url:
            body = e.get("response", {}).get("content", {}).get("text", "")
            if body:
                try:
                    return json.loads(body)
                except ValueError:
                    pass
    return None


def build_from_demandstar(days, bids, entity_name, entity_type,
                          keep_all=False):
    """Turn DemandStar bid dicts into classified, in-window records.
    Only 'Active' bids are open; other statuses are closed/under evaluation.
    """
    records = []
    for b in bids:
        if b["status"] and b["status"].lower() != "active":
            continue
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "DemandStar portal",
            "url":        b["url"],
        })
    return records


# ----- BidNet Direct (server-rendered 'sol-table' solicitation list) --------
def parse_bidnet(page_text):
    """Parse a BidNet Direct agency solicitation list.

    BidNet renders solicitations server-side into a <table class="sol-table
    mets-table">. Each data row (class mets-table-row) carries a sol-num,
    a sol-title link, and a 'Closing' date-value. The placeholder row
    (mets-table-row-empty) has no title link, so it is skipped naturally.
    """
    m = re.search(r'<table[^>]*sol-table[^>]*>(.*?)</table>', page_text, re.S)
    if not m:
        return []
    out = []
    rows = re.findall(
        r'<tr[^>]*class="[^"]*mets-table-row[^"]*"[^>]*>(.*?)</tr>',
        m.group(1), re.S)
    for r in rows:
        r = re.sub(r"<script[^>]*>.*?</script>", " ", r, flags=re.S)
        am = re.search(r'<div class="sol-title">\s*<a[^>]*href="([^"]+)"'
                       r'[^>]*>(.*?)</a>', r, re.S)
        if not am:
            continue
        title = re.sub(r"\s+", " ",
                       html.unescape(re.sub(r"<[^>]+>", "", am.group(2)))).strip()
        if not title:
            continue
        nm = re.search(r'<div class="sol-num">\s*(.*?)\s*</div>', r, re.S)
        num = (re.sub(r"\s+", " ",
                      html.unescape(re.sub(r"<[^>]+>", "", nm.group(1)))).strip()
               if nm else "")
        dm = re.search(r'Closing</span>\s*<span[^>]*class="date-value"[^>]*>'
                       r'\s*(\d{1,2}/\d{1,2}/\d{4})', r)
        out.append({
            "bid":   num,
            "title": title,
            "due":   _mdy_to_iso(dm.group(1)) if dm else "",
            "url":   html.unescape(am.group(1)),
        })
    return out


def build_from_bidnet(days, page_text, entity_name, entity_type,
                      keep_all=False):
    """Turn a BidNet Direct solicitation list into classified records."""
    records = []
    for b in parse_bidnet(page_text):
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "BidNet Direct portal",
            "url":        b["url"],
        })
    return records


# ----- OpenGov Procurement (public project API) -----------------------------
OPENGOV_API = ("https://api.procurement.opengov.com/api/v1/government/"
               "%s/project/public")


def opengov_slug(text):
    """Find the OpenGov government slug in a page OR a URL. A city's CMS
    bid page often just embeds an OpenGov portal in an iframe; sometimes
    the slug is only in the portal URL itself.
    """
    m = re.search(r"procurement\.opengov\.com/portal/embed/([A-Za-z0-9_\-]+)",
                  text or "")
    if m:
        return m.group(1)
    m = re.search(r"procurement\.opengov\.com/portal/([A-Za-z0-9_\-]+)",
                  text or "")
    if m and m.group(1).lower() not in ("embed", "static", "assets", "api"):
        return m.group(1)
    return ""


def fetch_opengov(slug, insecure=False):
    """Fetch an OpenGov agency's project list from its public API.

    Same unauthenticated endpoint OpenGov's own public portal uses; it
    just needs ordinary browser headers. Returns the newest ~50 projects,
    which reliably covers everything currently open.
    """
    text = fetch(OPENGOV_API % slug, insecure=insecure, extra_headers={
        "Accept":  "application/json",
        "Origin":  "https://procurement.opengov.com",
        "Referer": "https://procurement.opengov.com/",
    })
    return json.loads(text)


def parse_opengov_bids(data):
    """Extract bid dicts from an OpenGov project/public JSON response."""
    rows = data.get("rows") if isinstance(data, dict) else (
        data if isinstance(data, list) else None)
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        gov = r.get("government")
        slug = gov.get("code") if isinstance(gov, dict) else ""
        out.append({
            "bid":    str(r.get("financialId") or rid or "").strip(),
            "title":  (r.get("title") or "").strip(),
            "status": (r.get("status") or "").strip(),
            "due":    str(r.get("proposalDeadline") or "")[:10],
            "url":    ("https://procurement.opengov.com/portal/%s/projects/%s"
                       % (slug, rid)) if (slug and rid) else "",
        })
    return out


def extract_opengov_from_har(har_text):
    """Pull the OpenGov project/public JSON response out of a saved HAR."""
    try:
        har = json.loads(har_text)
    except ValueError:
        return None
    for e in har.get("log", {}).get("entries", []):
        url = e.get("request", {}).get("url", "")
        if "opengov" in url and "project/public" in url:
            body = e.get("response", {}).get("content", {}).get("text", "")
            if body:
                try:
                    return json.loads(body)
                except ValueError:
                    pass
    return None


def build_from_opengov(days, bids, entity_name, entity_type, keep_all=False):
    """Turn OpenGov bid dicts into classified, in-window records.
    Only status 'open' projects are currently accepting proposals.
    """
    records = []
    for b in bids:
        if b["status"].lower() != "open":
            continue
        category = classify(b["title"])
        if not category:
            if not keep_all:
                continue
            category = "Other / Uncategorized"
        if not within_window(b["due"], days):
            continue
        records.append({
            "bid":        b["bid"] or "(no number)",
            "title":      b["title"],
            "category":   category,
            "entity":     entity_name,
            "entityType": entity_type,
            "due":        b["due"],
            "source":     "OpenGov portal",
            "url":        b["url"],
        })
    return records


def har_payload(har_text):
    """Inspect a browser HAR capture and return (platform, payload):
      ("demandstar", <parsed JSON>)  -- the agency/search API response
      ("ionwave",    <html string>)  -- the rendered SourcingEvents page
      (None, None)                   -- nothing usable found
    """
    data = extract_demandstar_from_har(har_text)
    if data is not None:
        return "demandstar", data
    data = extract_opengov_from_har(har_text)
    if data is not None:
        return "opengov", data
    try:
        har = json.loads(har_text)
    except ValueError:
        return None, None
    for e in har.get("log", {}).get("entries", []):
        url = e.get("request", {}).get("url", "")
        mime = e.get("response", {}).get("content", {}).get("mimeType", "")
        if "html" not in mime.lower():
            continue
        body = e.get("response", {}).get("content", {}).get("text", "")
        if not body:
            continue
        if "ionwave.net" in url and "rgrow" in body.lower():
            return "ionwave", body
        if "bidnetdirect.com" in url and "sol-table" in body.lower():
            return "bidnet", body
    return None, None


def platform_from_url(url):
    """Best-effort eProcurement-platform label for a portal URL. Used to
    annotate the portal directory; does not need to fetch anything."""
    u = (url or "").lower()
    for needle, label in (
            ("bonfirehub", "Bonfire"),
            ("ionwave", "Ionwave"),
            ("demandstar", "DemandStar"),
            ("bidnetdirect", "BidNet Direct"),
            ("procurement.opengov.com", "OpenGov"),
            ("procureware", "ProcureWare"),
            ("texasbids", "texasbids.net"),
            ("beaconbid", "BeaconBid"),
            ("planetbids", "PlanetBids"),
            ("questcdn", "QuestCDN")):
        if needle in u:
            return label
    return ""


def normalize_entity_name(name, etype):
    """Make the entity name match the console's Portal Directory labels."""
    name = name.strip()
    low = name.lower()
    if etype == "city" and not low.startswith("city of"):
        return "City of " + name
    if etype == "county" and not low.endswith("county"):
        return name + " County"
    return name


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build(days, page_text):
    rows = parse_rows(page_text)
    records = []
    for r in rows:
        category = classify(r["desc"])
        if not category:
            continue
        if not within_window(r["deadline"], days):
            continue
        name, etype = resolve_entity(r["org"])
        records.append({
            "bid":        r["bid"],
            "title":      r["desc"],
            "category":   category,
            "entity":     name,
            "entityType": etype,
            "due":        r["deadline"],
            "source":     "Texas SmartBuy / ESBD",
            "url":        ESBD_PORTAL,
        })
    records.sort(key=lambda x: x["due"])
    return records


def main():
    ap = argparse.ArgumentParser(
        description="Refresh the TX traffic RFP data file (ESBD + city portals).")
    ap.add_argument("--days", type=int, default=182,
                    help="look-ahead window in days (default 182)")
    ap.add_argument("--out", default="rfp_data.json", help="output JSON path")
    ap.add_argument("--from-file", default=None,
                    help="parse a saved ESBD HTML dump instead of fetching")
    ap.add_argument("--insecure", action="store_true",
                    help="skip SSL certificate verification (last resort)")
    ap.add_argument("--no-esbd", action="store_true",
                    help="skip the ESBD feed; use only --portal-file inputs")
    ap.add_argument("--portal-file", action="append", default=[], metavar="SPEC",
                    help='add a city/county portal page. SPEC is '
                         '"Name::type::path-or-url", e.g. '
                         '"City of McAllen::city::McAllen.html". Repeatable.')
    ap.add_argument("--keep-all", action="store_true",
                    help="keep non-traffic portal bids too (category 'Other')")
    ap.add_argument("--portal-list", action="append", default=[], metavar="FILE",
                    help="a text file of portal entries, one "
                         '"Name::type::path-or-url" per line (# = comment). '
                         "Use this instead of many --portal-file flags.")
    ap.add_argument("--portal-dir", action="append", default=[], metavar="DIR",
                    help="process every *.html file in DIR. Each file's NAME "
                         "(minus .html) becomes the jurisdiction, e.g. "
                         "'City of McAllen.html' or 'Harris County.html'.")
    ap.add_argument("--merge", default=None, metavar="JSON",
                    help="merge into an existing rfp_data.json (dedupe by bid)")
    ap.add_argument("--render", action="store_true",
                    help="fetch live portal URLs with a headless browser so "
                         "JavaScript-built bid grids load. Needs Playwright "
                         "(pip install playwright; playwright install "
                         "chromium). Slower; does not defeat anti-bot walls.")
    args = ap.parse_args()

    if args.render and not _HAVE_PLAYWRIGHT:
        sys.exit(
            "ERROR: --render needs Playwright, which is not installed.\n"
            "Install it once:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "Then re-run with --render. Without --render, live URLs are\n"
            "fetched with plain HTTP (fast, but JavaScript portals return\n"
            "no bid data -- save those pages instead).")
    if args.render:
        print("Headless-browser rendering ON (Playwright). This is slower; "
              "each URL launches a browser.")

    records = []

    # ----- ESBD feed (state agencies + SmartBuy member locals) --------------
    if not args.no_esbd:
        if args.from_file:
            with open(args.from_file, encoding="utf-8") as fh:
                page = fh.read()
            print("Parsing local ESBD file:", args.from_file)
        else:
            print("Fetching ESBD:", PBT_OPEN_BIDS)
            try:
                page = fetch(PBT_OPEN_BIDS, insecure=args.insecure)
            except Exception as exc:                   # noqa: BLE001
                msg = str(exc)
                if "CERTIFICATE_VERIFY" in msg.upper() or "SSL" in msg.upper():
                    sys.exit(
                        "ERROR: SSL certificate verification failed.\n"
                        "  %s\n\n"
                        "This is the common macOS Python issue (python.org "
                        "builds\ndo not trust the system keychain). Fix with "
                        "ONE of:\n"
                        "  1) pip install certifi   <- recommended, then re-run\n"
                        "  2) run 'Install Certificates.command' from your\n"
                        "     /Applications/Python 3.x/ folder\n"
                        "  3) re-run with --insecure   (last resort)\n"
                        "Or skip the feed entirely with --no-esbd if you only\n"
                        "want to process --portal-file inputs.\n" % msg)
                sys.exit("ERROR fetching feed: %s\n"
                         "Run where publicbidtracker.com / txsmartbuy.gov are "
                         "reachable, or use --from-file / --no-esbd." % msg)
        esbd = build(args.days, page)
        print("  ESBD: %d traffic-related open solicitation(s)." % len(esbd))
        records.extend(esbd)

    # ----- city / county portal pages ---------------------------------------
    # Specs come from three sources. If the same jurisdiction appears in more
    # than one, a SAVED file wins over a live URL -- so you can keep a full
    # URL manifest AND drop in saved pages as you collect them.
    file_specs = list(args.portal_file)

    dir_specs = []
    for d in args.portal_dir:
        dd = os.path.expanduser(d)
        if not os.path.isdir(dd):
            print('  SKIP portal dir "%s": not a directory.' % dd)
            continue
        files = sorted(f for f in os.listdir(dd)
                       if f.lower().endswith((".html", ".htm", ".har")))
        for fn in files:
            stem = os.path.splitext(fn)[0]
            etype = "county" if "county" in stem.lower() else "city"
            dir_specs.append("%s::%s::%s" % (stem, etype,
                                             os.path.join(dd, fn)))
        print("Loaded portal dir: %s (%d file(s))" % (dd, len(files)))

    list_specs = []
    for listfile in args.portal_list:
        lf = os.path.expanduser(listfile)
        if not os.path.isfile(lf):
            print('  SKIP portal list "%s": file not found.' % lf)
            continue
        n0 = len(list_specs)
        with open(lf, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    list_specs.append(line)
        print("Loaded portal list: %s (%d entries)" % (lf, len(list_specs) - n0))

    # de-duplicate by jurisdiction name; priority: --portal-file, dir, list
    portal_specs, seen, dropped = [], set(), 0
    for source in (file_specs, dir_specs, list_specs):
        for spec in source:
            parts = spec.split("::")
            if len(parts) == 3:
                key = normalize_entity_name(parts[0].strip(),
                                            parts[1].strip().lower()).lower()
            else:
                key = spec
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            portal_specs.append(spec)
    if dropped:
        print("De-duplicated %d portal entr(ies) -- a saved page is used "
              "instead of the live URL." % dropped)

    total = len(portal_specs)
    audit = []                       # (name, reason) for the end-of-run summary

    def classify_fetch_error(exc):
        m = str(exc)
        if "404" in m:
            return "dead link (404) - URL has moved"
        if "403" in m or "Forbidden" in m or "reset by peer" in m:
            return "blocked by the site (anti-bot)"
        if "CERTIFICATE" in m.upper() or "SSL" in m.upper():
            return "TLS/certificate error"
        return "fetch error"

    for n, spec in enumerate(portal_specs, 1):
        tag = "[%d/%d] " % (n, total) if total > 1 else ""
        parts = spec.split("::")
        if len(parts) != 3:
            print('  %sSKIP bad manifest line: %r' % (tag, spec))
            audit.append(("?", "bad manifest line"))
            continue
        name, etype, path = (p.strip() for p in parts)
        etype = etype.lower()
        name = normalize_entity_name(name, etype)

        is_url = path.lower().startswith("http")

        # DemandStar pages are empty JavaScript shells -- fetch the public
        # JSON API directly (using the GUID in the URL) instead of the page.
        if is_url and "demandstar.com" in path.lower():
            guid = demandstar_guid(path)
            if guid:
                print("%sDemandStar API: %s" % (tag, name))
                try:
                    data = fetch_demandstar(guid, insecure=args.insecure)
                    recs = build_from_demandstar(
                        args.days, parse_demandstar_bids(data),
                        name, etype, keep_all=args.keep_all)
                    print("  OK   %s: %d DemandStar record(s) (via API)."
                          % (name, len(recs)))
                    records.extend(recs)
                    audit.append((name,
                                  "DemandStar API - parsed (%d)" % len(recs)))
                except Exception as exc:               # noqa: BLE001
                    print("  SKIP %s: DemandStar API fetch failed (%s)."
                          % (name, classify_fetch_error(exc)))
                    audit.append((name, "DemandStar - API fetch failed"))
                continue

        if is_url:
            how = "rendering" if args.render else "fetching"
            print("%s%s: %s" % (tag, how.capitalize(), name))
            try:
                if args.render:
                    ptext = fetch_rendered(path, insecure=args.insecure)
                else:
                    ptext = fetch(path, insecure=args.insecure)
            except Exception as exc:                   # noqa: BLE001
                reason = classify_fetch_error(exc)
                print("  SKIP %s: %s" % (name, reason))
                audit.append((name, reason))
                continue
        else:
            path = os.path.expanduser(path)
            if not os.path.isfile(path):
                print('  %sSKIP %s: saved file not found (%s)' % (tag, name, path))
                audit.append((name, "saved file not found"))
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                ptext = fh.read()
            print("%sReading: %s" % (tag, name))

        # --- browser HAR capture: pull the API/grid payload out of it -------
        if not is_url and path.lower().endswith(".har"):
            har_plat, payload = har_payload(ptext)
            if har_plat == "demandstar":
                recs = build_from_demandstar(
                    args.days, parse_demandstar_bids(payload),
                    name, etype, keep_all=args.keep_all)
                print("  OK   %s: %d DemandStar record(s) (from HAR)."
                      % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "DemandStar HAR - parsed (%d)" % len(recs)))
            elif har_plat == "ionwave":
                recs = build_from_ionwave(args.days, payload, name, etype,
                                          keep_all=args.keep_all)
                print("  OK   %s: %d Ionwave record(s) (from HAR)."
                      % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "Ionwave HAR - parsed (%d)" % len(recs)))
            elif har_plat == "bidnet":
                recs = build_from_bidnet(args.days, payload, name, etype,
                                         keep_all=args.keep_all)
                print("  OK   %s: %d BidNet Direct record(s) (from HAR)."
                      % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "BidNet HAR - parsed (%d)" % len(recs)))
            elif har_plat == "opengov":
                recs = build_from_opengov(args.days,
                                          parse_opengov_bids(payload),
                                          name, etype, keep_all=args.keep_all)
                print("  OK   %s: %d OpenGov record(s) (from HAR)."
                      % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "OpenGov HAR - parsed (%d)" % len(recs)))
            else:
                print("  SKIP %s: .har capture has no recognised bid data."
                      % name)
                audit.append((name, "HAR - no usable data"))
            continue

        plat = detect_platform(ptext, path)
        if plat == "procureware":
            recs = build_from_procureware(args.days, ptext, name, etype,
                                          keep_all=args.keep_all)
            if recs or "fullbidview_" in ptext.lower():
                print("  OK   %s: %d ProcureWare record(s)." % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "ProcureWare - parsed (%d)" % len(recs)))
            else:
                # ProcureWare site, but the live page is a JS shell with no grid
                print("  SKIP %s: ProcureWare site, but the LIVE page is\n"
                      "       JavaScript-rendered -- no bid rows in the HTML.\n"
                      "       Save the page in your browser (that captures the\n"
                      "       rendered grid) and use the saved .html file." % name)
                audit.append((name, "ProcureWare - needs SAVED page (JS)"))
        elif plat == "demandstar":
            print("  SKIP %s: DemandStar page saved as HTML has no bid data\n"
                  "       (it is a JavaScript shell). Use the agency URL in\n"
                  "       portals.txt, or a saved .har capture instead." % name)
            audit.append((name, "DemandStar - need URL or HAR"))
        elif plat == "napc":
            recs = build_from_napc(args.days, ptext, name, etype,
                                   keep_all=args.keep_all)
            print("  OK   %s: %d record(s) from texasbids.net aggregator."
                  % (name, len(recs)))
            records.extend(recs)
            audit.append((name, "texasbids.net - parsed (%d)" % len(recs)))
        elif plat == "bonfire":
            recs = build_from_bonfire(args.days, ptext, name, etype,
                                      keep_all=args.keep_all)
            print("  OK   %s: %d Bonfire record(s)." % (name, len(recs)))
            records.extend(recs)
            audit.append((name, "Bonfire - parsed (%d)" % len(recs)))
        elif plat == "civicplus":
            recs = build_from_civicplus(args.days, ptext, name, etype,
                                        keep_all=args.keep_all)
            if recs or "biditems" in ptext.lower():
                print("  OK   %s: %d CivicPlus record(s)." % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "CivicPlus - parsed (%d)" % len(recs)))
            else:
                print("  SKIP %s: CivicPlus site, but this page has no bid\n"
                      "       module. Save the actual 'Bids' page -- the one\n"
                      "       that shows the list of bids." % name)
                audit.append((name, "CivicPlus - not the bids page"))
        elif plat == "ionwave":
            recs = build_from_ionwave(args.days, ptext, name, etype,
                                      source_url=(path if is_url else ""),
                                      keep_all=args.keep_all)
            if recs or "rgmastertable" in ptext.lower():
                print("  OK   %s: %d Ionwave record(s)." % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "Ionwave - parsed (%d)" % len(recs)))
            else:
                print("  SKIP %s: Ionwave page, but no bid grid in the HTML.\n"
                      "       Save the SourcingEvents page after the grid\n"
                      "       loads, or fetch it with --render." % name)
                audit.append((name, "Ionwave - no grid in page"))
        elif plat == "bidnet":
            recs = build_from_bidnet(args.days, ptext, name, etype,
                                     keep_all=args.keep_all)
            if recs or "sol-table" in ptext.lower():
                print("  OK   %s: %d BidNet Direct record(s)." % (name, len(recs)))
                records.extend(recs)
                audit.append((name, "BidNet - parsed (%d)" % len(recs)))
            else:
                print("  SKIP %s: BidNet Direct site, but no solicitation list\n"
                      "       on this page. Save the agency's open-bids page." % name)
                audit.append((name, "BidNet - not the listing page"))
        elif plat == "opengov":
            slug = opengov_slug(ptext) or opengov_slug(path)
            if not slug:
                print("  SKIP %s: OpenGov embed found, but no portal slug\n"
                      "       in the page." % name)
                audit.append((name, "OpenGov - no slug found"))
            else:
                try:
                    data = fetch_opengov(slug, insecure=args.insecure)
                    recs = build_from_opengov(args.days,
                                              parse_opengov_bids(data),
                                              name, etype,
                                              keep_all=args.keep_all)
                    print("  OK   %s: %d OpenGov record(s) (via API)."
                          % (name, len(recs)))
                    records.extend(recs)
                    audit.append((name,
                                  "OpenGov API - parsed (%d)" % len(recs)))
                except Exception as exc:               # noqa: BLE001
                    print("  SKIP %s: OpenGov API fetch failed (%s)."
                          % (name, classify_fetch_error(exc)))
                    audit.append((name, "OpenGov - API fetch failed"))
        elif plat == "publicpurchase":
            print("  SKIP %s: publicpurchase portal -- no adapter yet." % name)
            audit.append((name, "publicpurchase - no adapter yet"))
        else:
            print("  SKIP %s: unrecognized page (not a known bid platform)."
                  % name)
            audit.append((name, "unrecognized page"))

    # ----- de-duplicate records --------------------------------------------
    # A bid can be listed under several categories on one page; keep it once.
    seen_rec, unique = set(), []
    for r in records:
        key = (r.get("entity", ""), r.get("bid", ""), r.get("title", ""))
        if key in seen_rec:
            continue
        seen_rec.add(key)
        unique.append(r)
    if len(unique) != len(records):
        print("Removed %d duplicate record(s)." % (len(records) - len(unique)))
    records = unique

    # ----- end-of-run portal audit ------------------------------------------
    if total:
        portal_sources = {"ProcureWare portal", "CivicPlus portal",
                          "Bonfire portal", "texasbids.net aggregator",
                          "DemandStar portal", "Ionwave portal",
                          "BidNet Direct portal", "OpenGov portal"}
        kept = sum(1 for r in records if r.get("source") in portal_sources)
        print("\n" + "=" * 60)
        print("PORTAL RUN SUMMARY  (%d processed, %d bid record(s) kept)"
              % (total, kept))
        print("=" * 60)
        tally = {}
        for _, reason in audit:
            key = reason.split(" (")[0].split(" - ")[0]
            tally[key] = tally.get(key, 0) + 1
        for key in sorted(tally, key=lambda k: -tally[k]):
            print("  %3d  %s" % (tally[key], key))
        print("=" * 60)

    # ----- merge with an existing file --------------------------------------
    if args.merge:
        try:
            with open(args.merge, encoding="utf-8") as fh:
                prev = json.load(fh).get("records", [])
            have = {r["bid"] for r in records}
            kept = [r for r in prev if r.get("bid") not in have]
            records.extend(kept)
            print("Merged %d existing record(s) from %s." % (len(kept), args.merge))
        except (OSError, ValueError) as exc:
            print("WARNING: could not merge %s (%s)" % (args.merge, exc))

    if not records:
        print("No records produced. Check your inputs (--portal-list / "
              "--portal-file / ESBD).")

    records.sort(key=lambda x: x.get("due", ""))

    n_portal = sum(1 for r in records if r.get("source") == "ProcureWare portal")
    src = "Texas SmartBuy ESBD (via Public Bid Tracker mirror)"
    if n_portal:
        src += " + %d city-portal record(s)" % n_portal
    if args.no_esbd:
        src = "City-portal records (%d)" % n_portal

    # Portal directory: emit the exact portal list the scraper used, so the
    # console's "Portal Directory" tab stays in lockstep with portals.txt
    # (no more drift between the manifest and the UI's hard-coded list).
    portal_dir = []
    for spec in portal_specs:
        parts = spec.split("::")
        if len(parts) != 3:
            continue
        pn, pt, pp = (x.strip() for x in parts)
        if not pp.lower().startswith("http"):
            continue
        portal_dir.append({
            "name":     normalize_entity_name(pn, pt.lower()),
            "type":     pt,
            "url":      pp,
            "platform": platform_from_url(pp),
        })

    payload = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "window_days": args.days,
        "source": src,
        "note": ("ESBD covers state agencies + Texas SmartBuy member "
                 "cities/counties. Other cities are pulled from their own "
                 "portals via --portal-file adapters."),
        "count": len(records),
        "portals": portal_dir,
        "records": records,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print("Total: %d record(s). Wrote: %s" % (len(records), args.out))
    print('Load this file in the console\'s "Open Procurements" tab.')


if __name__ == "__main__":
    main()
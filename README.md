# Texas Procurement Portal

**Open Procurements · Traffic / ITS / Signal Timing**

A self-hosted tool for discovering open traffic-engineering RFPs — signals,
ITS, detection, signal timing, transportation planning, signing & marking,
active transportation — across Texas cities and counties.

It has two parts:

- **`tx_rfp_scraper.py`** — a Python scraper that reads the statewide ESBD
  feed plus each jurisdiction's own procurement portal, classifies every
  solicitation by traffic-engineering category, and writes `rfp_data.json`.
- **`index.html`** — a standalone browser console that loads `rfp_data.json`
  and presents it as a filterable list of open procurements plus a directory
  of every jurisdiction's bid portal.

There is no server and no build step. The scraper is one Python file; the
console is one HTML file you open directly in a browser.

## What it covers

The scraper pulls from the Texas SmartBuy / ESBD feed and parses eight
eProcurement platforms automatically:

| Platform | How it is read |
|---|---|
| ProcureWare | rendered page / saved page |
| CivicPlus | server-rendered bid module |
| Bonfire | rendered page / saved page |
| Ionwave | server-rendered Telerik grid |
| texasbids.net | server-rendered tables |
| DemandStar | public JSON API (or a HAR capture) |
| OpenGov | public JSON API (or a HAR capture) |
| BidNet Direct | server-rendered solicitation list |

Platforms without an adapter yet (BeaconBid, PlanetBids, QuestCDN) are
reported with a `SKIP` line rather than failing silently.

## Requirements

- **Python 3.9+** — the scraper uses only the standard library for a basic
  run.
- **Playwright** *(optional)* — only needed for `--render`, which loads
  JavaScript-built portals in a headless browser:

  ```
  pip install playwright
  playwright install chromium
  ```

## Quick start

```
chmod +x refresh.sh         # first time only
./refresh.sh                # ESBD + saved pages + portals.txt URLs
```

Then open `index.html` in a browser and use **Load rfp_data.json** to load
the file the run just produced.

### refresh.sh options

| Command | What it does |
|---|---|
| `./refresh.sh` | ESBD feed + saved `html/` pages + live `portals.txt` URLs |
| `./refresh.sh --render` | also render JavaScript portals in a headless browser (slow; needs Playwright) |
| `./refresh.sh --fast` | ESBD + saved `html/` pages only (skip the URL list) |
| `./refresh.sh --no-esbd` | skip the ESBD fetch (portals only) |

`refresh.sh` activates a local virtualenv if it finds one, backs up the
previous `rfp_data.json` into `backups/` (keeping the last 10), and prints
exactly what it ran.

## The portal manifest — `portals.txt`

One jurisdiction per line:

```
Name::type::path-or-url
```

- `type` is `city` or `county`.
- `path-or-url` may be a live portal URL, a saved `.html` page, or a `.har`
  capture. A saved page, when present, is used in preference to the live URL.

`portals.txt` is the single source of truth: the scraper writes the portal
list it used into `rfp_data.json`, and the console's **Portal Directory** tab
is built from that list — so the directory can no longer drift from the
manifest.

## The console — `index.html`

- **Open Procurements** — every open solicitation from the loaded
  `rfp_data.json`, filterable by closing window and traffic category, with
  CSV and PDF export.
- **Portal Directory & Search** — every jurisdiction's official bid portal,
  built from the `portals` list in `rfp_data.json`; selecting a jurisdiction
  shows its open bids inline.

The console ships with an embedded data snapshot so it is usable before the
first scraper run.

## Capturing HAR files (DemandStar / OpenGov)

DemandStar and OpenGov serve bids from a JSON API; the scraper calls those
APIs directly. If a direct call is ever blocked, you can instead capture a
HAR in your browser (DevTools → Network → save as HAR) and point the manifest
at the `.har` file. This is optional — the direct API path is the default.

## Limitations — read this

This tool is honest about what it can and cannot do:

- **It is a snapshot, not a live feed.** Data is current as of the run. Open
  the console a week later without re-running and it is a week stale.
- **It is not real-time.** To be notified the moment a bid posts, register
  for free vendor alerts on BidNet Direct and DemandStar; that is the
  intended way to never miss a new solicitation.
- **Live fetches fail for some sites.** Expect a number of dead (404) links
  and anti-bot blocks on any full run; the run summary lists each one.
  `--render` recovers some blocked sites but not all, and never fixes a 404.
- **The result set is small by nature.** Across ~90 Texas jurisdictions only
  a handful of *traffic* RFPs are open at any given time. A small list is the
  base rate, not a failure of the tool.

Use the scraper and console to organize and track the bids you are pursuing;
use the aggregators' free email alerts as the discovery net.

## Repository layout

```
tx_rfp_scraper.py   the scraper
index.html          the console (open in a browser)
portals.txt         the jurisdiction manifest
refresh.sh          one-command runner
README.md           this file
.gitignore
rfp_data.json       generated by the scraper (git-ignored)
backups/            previous rfp_data.json files (git-ignored)
html/               optional saved portal pages / HAR files (git-ignored)
```

## Notes

Government procurement listings are public information. DemandStar, OpenGov,
and BidNet Direct are commercial services; this tool reads the same public
endpoints their own public pages use, for occasional personal use. It does
not bypass logins or anti-bot protections. Verify every solicitation on the
issuing portal before relying on it.

#!/usr/bin/env python3
"""
Tampa Bay Networking Calendar aggregator.

Reads sources.yaml, pulls events from each organization (feeds, site APIs or
event pages), filters them, merges them, and writes docs/networking.ics plus a
docs/status.html health report. A source that breaks never breaks the build:
its last good events are reused from docs/cache/ until it is fixed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dparser
from dateutil import tz
from icalendar import Calendar, Event

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
CACHE = DOCS / "cache"
ET = tz.gettz("America/New_York")
UTC = tz.UTC
TZ_ABBR = {"EDT": ET, "EST": ET, "ET": ET, "UTC": UTC, "GMT": UTC}
NOW = dt.datetime.now(UTC)
WINDOW_START = NOW - dt.timedelta(days=30)
WINDOW_END = NOW + dt.timedelta(days=400)
CACHE_MAX_AGE_DAYS = 21
MAX_DETAIL_PAGES = 60

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
}

# curl_cffi sends a real Chrome TLS fingerprint, which gets past most Cloudflare
# "bot" rules that block plain Python requests from cloud servers like GitHub's.
try:
    from curl_cffi import requests as cffi_requests
    SESSION = cffi_requests.Session(impersonate="chrome")
except Exception:  # pragma: no cover - fallback when curl_cffi is unavailable
    SESSION = requests.Session()
    SESSION.headers.update(BROWSER_HEADERS)


class FetchError(RuntimeError):
    pass


def fetch(url: str, **kw):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code} from {url.split('?')[0]}"
            if r.status_code in (403, 404, 410):
                break
        except Exception as ex:
            last = f"{ex.__class__.__name__} on {url.split('?')[0]}"
        time.sleep(2 * (attempt + 1))
    raise FetchError(last)


# ---------------------------------------------------------------- data model
@dataclass
class Ev:
    org: str
    title: str
    start: str            # ISO datetime (aware) or ISO date when all_day
    end: str | None = None
    all_day: bool = False
    url: str = ""
    location: str = ""
    source_id: str = ""
    recurring: bool = False

    def start_dt(self) -> dt.datetime:
        if self.all_day:
            d = dt.date.fromisoformat(self.start[:10])
            return dt.datetime(d.year, d.month, d.day, tzinfo=ET)
        return dt.datetime.fromisoformat(self.start)


def mk_event(org, title, start, end=None, all_day=False, **kw) -> Ev:
    if all_day:
        return Ev(org, clean(title), start.date().isoformat() if isinstance(start, dt.datetime) else start.isoformat(),
                  None, True, **kw)
    if start.tzinfo is None:
        start = start.replace(tzinfo=ET)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=ET)
    if end is not None and end <= start:
        end = None
    return Ev(org, clean(title), start.isoformat(), end.isoformat() if end else None, False, **kw)


def clean(s: str) -> str:
    s = BeautifulSoup(s or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- date parsing helpers
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATE_PATTERNS = [
    re.compile(rf"({MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})"),   # Nov 19, 2026 / November 19 2026
    re.compile(rf"(\d{{1,2}}\s+{MONTH}\s+\d{{4}})"),                      # 15 Apr 2026 (Wild Apricot)
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),                               # 11/19/2026
]
TIME = r"\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?"
TIME_RANGE = re.compile(rf"({TIME})\s*(?:-|–|—|to)\s*-?\s*({TIME})\s*(EDT|EST|ET|UTC|GMT)?")
SINGLE_TIME = re.compile(rf"({TIME})\s*(EDT|EST|ET|UTC|GMT)?")
ISO_PAIR = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)\s*-\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)")


def _norm_time(t: str) -> str:
    t = t.replace(".", "").upper().replace(" ", "")
    if ":" not in t:
        t = t[:-2] + ":00" + t[-2:]
    return t


def _combine(date_str: str, time_str: str | None, zone: str | None) -> dt.datetime:
    d = dparser.parse(re.sub(r"(st|nd|rd|th),", ",", date_str), fuzzy=True).date()
    if not time_str:
        return dt.datetime(d.year, d.month, d.day)
    t = dparser.parse(_norm_time(time_str)).time()
    return dt.datetime.combine(d, t, tzinfo=TZ_ABBR.get((zone or "ET").upper(), ET))


def parse_jsonld(soup: BeautifulSoup):
    """Return (title, start, end, location) from a schema.org Event block, if any."""
    def walk(o):
        if isinstance(o, list):
            for x in o:
                yield from walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(isinstance(x, str) and x.endswith("Event") for x in types) and o.get("startDate"):
                yield o
            for k in ("@graph", "itemListElement", "item"):
                if k in o:
                    yield from walk(o[k])
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "{}")
        except Exception:
            continue
        for ev in walk(data):
            start = dparser.isoparse(ev["startDate"])
            end = dparser.isoparse(ev["endDate"]) if ev.get("endDate") else None
            start = start if start.tzinfo else start.replace(tzinfo=ET)
            end = end if (end is None or end.tzinfo) else end.replace(tzinfo=ET)
            loc = ev.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            if isinstance(loc, dict):
                addr = loc.get("address")
                if isinstance(addr, dict):
                    addr = ", ".join(str(addr.get(k, "")) for k in
                                     ("streetAddress", "addressLocality", "addressRegion") if addr.get(k))
                loc = ", ".join(x for x in (loc.get("name"), addr if isinstance(addr, str) else "") if x)
            all_day = len(ev["startDate"]) <= 10
            return ev.get("name"), start, end, str(loc or ""), all_day
    return None


def parse_chambermaster(html: str, url: str):
    """ChamberMaster/WebLink event pages (e.g. web.abcflgulf.org) fill in the date with
    JavaScript from an embedded JSON model. Read that model directly."""
    m = re.search(r"-(\d+)/details", url)
    if not m:
        return None
    pos = html.find(f'"EventId":{m.group(1)}')
    if pos < 0:
        return None
    chunk = html[pos: pos + 4000]

    def g(k):
        mm = re.search(rf'"{k}"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
        return json.loads(f'"{mm.group(1)}"') if mm else ""
    date_s, name = g("EventDate"), g("EventName")
    if not date_s or not name:
        return None
    d = dt.date.fromisoformat(date_s[:10])
    t1, t2 = g("EventTime"), g("EndTime")
    loc = ", ".join(x for x in (g("Location"), g("Address1"), g("City")) if x)
    if not t1:
        return clean(name), dt.datetime(d.year, d.month, d.day), None, True, loc
    start = dt.datetime.combine(d, dparser.parse(_norm_time(t1)).time(), tzinfo=ET)
    end = dt.datetime.combine(d, dparser.parse(_norm_time(t2)).time(), tzinfo=ET) if t2 else None
    return clean(name), start, end, False, loc


def parse_detail_page(html: str, url: str):
    """Best-effort extraction of (title, start, end, all_day, location) from an event page."""
    soup = BeautifulSoup(html, "html.parser")
    title = None
    og = soup.find("meta", property="og:title")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif og and og.get("content"):
        title = og["content"]
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)

    ld = parse_jsonld(soup)
    if ld:
        ld_title, start, end, loc, all_day = ld
        return clean(ld_title or title or ""), start, end, all_day, loc

    cm = parse_chambermaster(html, url)
    if cm:
        return cm

    for s in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        s.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    # Start looking at the event title so sidebars of *other* events are less likely to win.
    anchor = text.find(title[:40]) if title else -1
    body = text[anchor:] if anchor >= 0 else text

    m = ISO_PAIR.search(body)
    if m:
        s_, e_ = (dparser.isoparse(m.group(i)) for i in (1, 2))
        s_, e_ = (x if x.tzinfo else x.replace(tzinfo=ET) for x in (s_, e_))
        return clean(title or ""), s_, e_, False, ""

    best = None
    for pat in DATE_PATTERNS:
        dm = pat.search(body)
        if dm and (best is None or dm.start() < best.start()):
            best = dm
    if not best:
        return None
    date_str = best.group(1)
    near = body[max(0, best.start() - 60): best.end() + 250]
    after = body[best.end(): best.end() + 250]
    tr = TIME_RANGE.search(after) or TIME_RANGE.search(near)
    if tr:
        start = _combine(date_str, tr.group(1), tr.group(3))
        end = _combine(date_str, tr.group(2), tr.group(3))
        return clean(title or ""), start, end, False, ""
    st = SINGLE_TIME.search(after)
    if st:
        return clean(title or ""), _combine(date_str, st.group(1), st.group(2)), None, False, ""
    return clean(title or ""), _combine(date_str, None, None), None, True, ""


# ---------------------------------------------------------------- source handlers
def src_tribe(src) -> list[Ev]:
    """WordPress 'The Events Calendar' sites: REST API first, iCal feed as fallback."""
    base = src["url"].rstrip("/")
    events: list[Ev] = []
    try:
        api = f"{base}/wp-json/tribe/events/v1/events"
        params = {"start_date": WINDOW_START.strftime("%Y-%m-%d"),
                  "end_date": WINDOW_END.strftime("%Y-%m-%d"), "per_page": 50}
        nxt, pages = api, 0
        while nxt and pages < 10:
            data = fetch(nxt, params=params if pages == 0 else None).json()
            for e in data.get("events", []):
                zone = tz.gettz(e.get("timezone") or "America/New_York") or ET
                start = dparser.parse(e["start_date"]).replace(tzinfo=zone)
                end = dparser.parse(e["end_date"]).replace(tzinfo=zone) if e.get("end_date") else None
                v = e.get("venue") or {}
                loc = ", ".join(x for x in (v.get("venue"), v.get("address"), v.get("city")) if x) if isinstance(v, dict) else ""
                title = clean(e.get("title", ""))
                if not title and e.get("url"):  # some events are saved without a title
                    try:
                        title = (parse_detail_page(fetch(e["url"]).text, e["url"]) or ("",))[0]
                    except Exception:
                        title = ""
                events.append(mk_event(src["org"], title or "Event (see link)", start, end,
                                       bool(e.get("all_day")), url=e.get("url", ""), location=clean(loc)))
            nxt, pages = data.get("next_rest_url"), pages + 1
        if events:
            return events
        api_err = "API returned no events"
    except Exception as ex:
        api_err = str(ex)
    try:
        return src_ics({**src, "url": src.get("ics_url") or f"{base}/events/?ical=1"})
    except Exception as ex:
        raise FetchError(f"API: {api_err}; iCal: {ex}")


def src_ics(src) -> list[Ev]:
    cal = Calendar.from_ical(fetch(src["url"]).content)
    out = []
    for comp in cal.walk("VEVENT"):
        s = comp.decoded("DTSTART")
        e = comp.decoded("DTEND") if comp.get("DTEND") else None
        all_day = not isinstance(s, dt.datetime)
        if all_day:
            s = dt.datetime(s.year, s.month, s.day)
        if isinstance(e, dt.date) and not isinstance(e, dt.datetime):
            e = None
        out.append(mk_event(src["org"], str(comp.get("SUMMARY", "")), s, e, all_day,
                            url=str(comp.get("URL", "")), location=str(comp.get("LOCATION", ""))))
    return out


def src_squarespace(src) -> list[Ev]:
    """Squarespace events collections expose JSON with ?format=json."""
    data = fetch(src["url"], params={"format": "json"}).json()
    items = data.get("upcoming") or data.get("items") or []
    out = []
    for it in items:
        start = dt.datetime.fromtimestamp(it["startDate"] / 1000, UTC)
        end = dt.datetime.fromtimestamp(it["endDate"] / 1000, UTC) if it.get("endDate") else None
        loc = it.get("location") or {}
        loc_s = ", ".join(x for x in (loc.get("addressTitle"), loc.get("addressLine1"), loc.get("addressLine2")) if x)
        out.append(mk_event(src["org"], it.get("title", ""), start, end,
                            url=urljoin(src["url"], it.get("fullUrl", "")), location=loc_s))
    return out


def expand_list_urls(src) -> list[str]:
    urls = []
    months = int(src.get("months", 1))
    for tmpl in src["list_urls"]:
        if any(k in tmpl for k in ("{ym}", "{y}", "{m}")):
            y, m = NOW.year, NOW.month
            for _ in range(months):
                urls.append(tmpl.format(ym=f"{y}-{m:02d}", y=y, m=m))
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        else:
            urls.append(tmpl)
    return urls


def src_link_crawl(src) -> list[Ev]:
    """Collect event-detail links from listing pages, then read each detail page."""
    link_re = re.compile(src["link_regex"])
    links: list[str] = []
    list_errors, last_err = 0, ""
    for lu in expand_list_urls(src):
        try:
            soup = BeautifulSoup(fetch(lu).text, "html.parser")
        except Exception as ex:
            list_errors += 1
            last_err = str(ex)
            continue
        for a in soup.find_all("a", href=True):
            href = requests.utils.requote_uri(urljoin(lu, a["href"].strip()).split("#")[0])
            if link_re.search(href) and href not in links:
                links.append(href)
    if list_errors == len(expand_list_urls(src)):
        raise FetchError(f"could not load any listing page ({last_err})")
    if not links:
        raise RuntimeError("no upcoming events found (listing loaded, but no event links matched link_regex)")
    out = []
    for url in links[:MAX_DETAIL_PAGES]:
        try:
            parsed = parse_detail_page(fetch(url).text, url)
        except Exception:
            continue
        if not parsed:
            continue
        title, start, end, all_day, loc = parsed
        if not title:
            continue
        out.append(mk_event(src["org"], title, start, end, all_day, url=url, location=loc))
    return out


def src_crew(src) -> list[Ev]:
    """CREW Network chapter sites load events from a JSON API shared by all chapters."""
    base = src["url"].rstrip("/")
    data = fetch(f"{base}/api/events", params={
        "minEndDate": WINDOW_START.strftime("%Y-%m-%d"),
        "maxEndDate": WINDOW_END.strftime("%Y-%m-%d")}).json()
    out = []
    for e in data:
        if e.get("chapterCode") != src["chapter_code"]:
            continue
        start = dparser.isoparse(e["startDateTime"])
        end = dparser.isoparse(e["endDateTime"]) if e.get("endDateTime") else None
        a = e.get("address") or {}
        loc = ", ".join(x.strip(" |") for x in (e.get("locationName"), a.get("line1"), a.get("city")) if x)
        out.append(mk_event(src["org"], e.get("title", ""), start, end,
                            url=e.get("registrationUrl") or f"{base}/events/view-all-events", location=loc))
    return out


def src_chambermaster(src) -> list[Ev]:
    """ChamberMaster/WebLink event calendars (e.g. web.abcflgulf.org) load events from an
    XML feed; times in it are UTC."""
    import xml.etree.ElementTree as ET_xml
    base = src["url"].rstrip("/")
    resp = fetch(f"{base}/External/WCControls/V12/WebDeps/Calendar/xml/newCalendarXML.aspx",
                 params={"startdate": WINDOW_START.strftime("%Y-%m-%d"),
                         "enddate": WINDOW_END.strftime("%Y-%m-%d")})
    root = ET_xml.fromstring(resp.content)
    out = []
    for n in root:
        g = lambda k: (n.findtext(k) or "").strip()
        title, sid = g("title"), g("id")
        if not title or not g("StartDateTimeUtc"):
            continue
        start = dparser.isoparse(g("StartDateTimeUtc"))
        end = dparser.isoparse(g("EndDateTimeUtc")) if g("EndDateTimeUtc") else None
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        end = end if (end is None or end.tzinfo) else end.replace(tzinfo=UTC)
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
        url = g("customDetailsUrl") or f"{base}/events/{slug}-{sid}/details"
        loc = ", ".join(x for x in (g("Venue"), g("Address1"), g("City")) if x)
        out.append(mk_event(src["org"], title, start, end, url=url, location=loc))
    return out


WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def src_recurring(src) -> list[Ev]:
    """Fixed monthly meetings, e.g. 'third Wednesday 3:30-5:00'."""
    out = []
    wd, nth = WEEKDAYS[src["weekday"]], int(src["nth"])
    sh, sm = map(int, src["start"].split(":"))
    eh, em = map(int, src["end"].split(":"))
    skip = set(src.get("skip_months", []))
    y, m = NOW.year, NOW.month
    for _ in range(int(src.get("months", 12))):
        if m not in skip:
            first = dt.date(y, m, 1)
            offset = (wd - first.weekday()) % 7
            day = first + dt.timedelta(days=offset + 7 * (nth - 1)) if nth > 0 else None
            if nth == -1:  # last weekday of month
                nxt = dt.date(y + (m == 12), (m % 12) + 1, 1)
                last = nxt - dt.timedelta(days=1)
                day = last - dt.timedelta(days=(last.weekday() - wd) % 7)
            if day and day.month == m:
                s = dt.datetime(day.year, day.month, day.day, sh, sm, tzinfo=ET)
                e = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=ET)
                ev = mk_event(src["org"], src["title"], s, e, url=src.get("url", ""),
                              location=src.get("location", ""))
                ev.recurring = True
                out.append(ev)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


HANDLERS = {"tribe": src_tribe, "ics": src_ics, "squarespace": src_squarespace,
            "link_crawl": src_link_crawl, "recurring": src_recurring, "crew": src_crew,
            "chambermaster": src_chambermaster}


# ---------------------------------------------------------------- pipeline
def keep(ev: Ev, src) -> bool:
    inc, exc = src.get("include_title_regex"), src.get("exclude_title_regex")
    if inc and not re.search(inc, ev.title):
        return False
    if exc and re.search(exc, ev.title):
        return False
    s = ev.start_dt()
    return WINDOW_START <= s <= WINDOW_END


def load_cache(sid):
    p = CACHE / f"{sid}.json"
    if not p.exists():
        return None, None
    d = json.loads(p.read_text())
    return [Ev(**e) for e in d["events"]], dt.datetime.fromisoformat(d["saved"])


def save_cache(sid, events):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / f"{sid}.json").write_text(json.dumps(
        {"saved": NOW.isoformat(), "events": [asdict(e) for e in events]}, indent=1))


def manual_events(path: Path) -> list[Ev]:
    if not path.exists():
        return []
    items = yaml.safe_load(path.read_text()) or []
    out = []
    for it in items:
        if not it:
            continue
        start = dparser.parse(str(it["start"]))
        end = dparser.parse(str(it["end"])) if it.get("end") else None
        all_day = len(str(it["start"])) <= 10
        ev = mk_event(it["org"], it["title"], start, end, all_day,
                      url=it.get("url", ""), location=it.get("location", ""))
        ev.source_id = "manual"
        out.append(ev)
    return out


def dedupe(events: list[Ev]) -> list[Ev]:
    seen, out = set(), []
    real_days = {(e.org, e.start_dt().date()) for e in events if not e.recurring}
    for e in sorted(events, key=lambda x: (x.recurring, x.start_dt())):
        if e.recurring and (e.org, e.start_dt().date()) in real_days:
            continue  # a real listing exists that day; trust it over the recurring rule
        key = (e.org, e.start_dt().date(), re.sub(r"\W+", "", e.title.lower())[:24])
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return sorted(out, key=lambda x: x.start_dt())


def build_ics(events: list[Ev], name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//Vertex//Tampa Bay Networking Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", "America/New_York")
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval", dt.timedelta(hours=6), parameters={"VALUE": "DURATION"})
    for e in events:
        ve = Event()
        ve.add("uid", hashlib.sha1(f"{e.org}|{e.title}|{e.start}".encode()).hexdigest() + "@tb-networking")
        ve.add("dtstamp", NOW)
        ve.add("summary", f"[{e.org}] {e.title}")
        if e.all_day:
            d = dt.date.fromisoformat(e.start[:10])
            ve.add("dtstart", d)
            ve.add("dtend", d + dt.timedelta(days=1))
        else:
            s = dt.datetime.fromisoformat(e.start).astimezone(UTC)
            en = dt.datetime.fromisoformat(e.end).astimezone(UTC) if e.end else s + dt.timedelta(hours=2)
            ve.add("dtstart", s)
            ve.add("dtend", en)
        if e.location:
            ve.add("location", e.location)
        if e.url:
            ve.add("url", e.url)
        desc = [f"Organization: {e.org}"]
        if e.recurring:
            desc.append("Standing monthly meeting (generated from the published schedule; confirm before going).")
        if e.url:
            desc.append(f"Details / registration: {e.url}")
        ve.add("description", "\n".join(desc))
        ve.add("categories", [e.org])
        ve.add("transp", "TRANSPARENT")  # show as Free so it never blocks your real availability
        cal.add_component(ve)
    return cal.to_ical()


def write_status(rows, total):
    DOCS.mkdir(exist_ok=True)
    (DOCS / "status.json").write_text(json.dumps({"built": NOW.isoformat(), "total": total, "sources": rows}, indent=1))
    colors = {"ok": "#1f7a3a", "cached": "#b26b00", "empty": "#b26b00", "error": "#b00020"}
    trs = "\n".join(
        f"<tr><td>{r['name']}</td><td>{r['type']}</td><td>{r['count']}</td>"
        f"<td style='color:{colors.get(r['state'], '#333')}'>{r['state']}</td><td>{r.get('note', '')}</td></tr>"
        for r in rows)
    built = NOW.astimezone(ET).strftime("%b %d, %Y %I:%M %p ET")
    (DOCS / "status.html").write_text(f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Networking calendar status</title>
<style>body{{font:15px system-ui,sans-serif;margin:24px;max-width:1000px}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}}th{{background:#f4f4f4}}</style>
<h1>Tampa Bay networking calendar</h1>
<p>Last built {built} &middot; {total} events in the feed &middot; subscribe URL: <code>networking.ics</code> in this folder</p>
<table><tr><th>Source</th><th>Method</th><th>Events</th><th>State</th><th>Note</th></tr>{trs}</table>""")
    (DOCS / "index.html").write_text('<meta http-equiv="refresh" content="0; url=status.html">')


def main() -> int:
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
    all_events: list[Ev] = []
    rows = []
    for src in cfg["sources"]:
        if src.get("disabled"):
            continue
        sid, row = src["id"], {"name": src["name"], "type": src["type"], "count": 0, "state": "ok"}
        try:
            got = [e for e in HANDLERS[src["type"]](src) if keep(e, src)]
            for e in got:
                e.source_id = sid
            if got:
                save_cache(sid, got)
                row["count"] = len(got)
            else:
                raise RuntimeError("no upcoming events found")
        except Exception as ex:
            cached, saved = load_cache(sid)
            msg = str(ex).splitlines()[0][:160] if str(ex) else ex.__class__.__name__
            if cached and saved and (NOW - saved).days <= CACHE_MAX_AGE_DAYS:
                got = [e for e in cached if keep(e, src)]
                row.update(state="cached", count=len(got), note=f"{msg} - using events saved {saved:%b %d}")
            else:
                got = []
                row.update(state="empty" if "no upcoming" in msg else "error", note=msg)
            if "--debug" in sys.argv:
                traceback.print_exc()
        all_events.extend(got)
        rows.append(row)
        print(f"{row['state']:>7}  {row['count']:>3}  {src['name']}  {row.get('note', '')}")

    manual = [e for e in manual_events(ROOT / "manual_events.yaml") if WINDOW_START <= e.start_dt() <= WINDOW_END]
    all_events.extend(manual)
    rows.append({"name": "Manual entries (manual_events.yaml)", "type": "manual", "count": len(manual), "state": "ok"})

    final = dedupe(all_events)
    DOCS.mkdir(exist_ok=True)
    (DOCS / "networking.ics").write_bytes(build_ics(final, cfg.get("calendar_name", "Tampa Bay Networking")))
    write_status(rows, len(final))
    print(f"\nWrote {len(final)} events to docs/networking.ics")
    return 0


if __name__ == "__main__":
    sys.exit(main())

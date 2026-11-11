"""Parser checks built from real page text seen on each org's site (Nov 2026)."""
import datetime as dt, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_calendar as bc

def page(title, body):
    return f"<html><body><h1>{title}</h1><div>{body}</div></body></html>"

CASES = {
 "TBBA (GrowthZone)": (page("2026 Awards of Excellence: 11/19/26",
    "Date and Time Thursday Nov 19, 2026 5:30 PM - 9:30 PM EST Location The Gathering Room Armature Works"),
    dt.datetime(2026,11,19,17,30), dt.datetime(2026,11,19,21,30)),
 "ULI (ISO)": (page("ULI Tampa Bay - 2026 Trends Conference",
    "When 2026-04-30 2026-04-30T12:00:00 - 2026-04-30T18:30:00 America/New_York Add to Calendar"),
    dt.datetime(2026,4,30,12,0), dt.datetime(2026,4,30,18,30)),
 "CREDA (Wild Apricot)": (page("NAIOP Market Update",
    "When 24 Oct 2024 4:00 PM - 7:30 PM Location JW Marriott Tampa Water Street"),
    dt.datetime(2024,10,24,16,0), dt.datetime(2024,10,24,19,30)),
 "Chamber": (page("Tampa Bay @ Sunset",
    "Date: September 30, 2026 Time: 5:30 pm - 7:30 pm Location: Che Vita"),
    dt.datetime(2026,9,30,17,30), dt.datetime(2026,9,30,19,30)),
 "CREW (UTC on page)": (page("Monthly Luncheon",
    "Wednesday, October 14, 2026 from 3:30 PM to - 5:00 PM UTC Feather Sound Country Club"),
    dt.datetime(2026,10,14,11,30), dt.datetime(2026,10,14,13,0)),
}

ok = True
for name, (html, exp_s, exp_e) in CASES.items():
    title, s, e, all_day, loc = bc.parse_detail_page(html, "https://x")
    s_et = s.astimezone(bc.ET).replace(tzinfo=None); e_et = e.astimezone(bc.ET).replace(tzinfo=None)
    good = (s_et, e_et) == (exp_s, exp_e)
    ok &= good
    print(("PASS" if good else "FAIL"), name, "|", title, "|", s_et, "->", e_et)

# JSON-LD path (SUCA/Tribe event pages expose this)
ld = '<script type="application/ld+json">[{"@type":"Event","name":"SUCA Clay Shoot","startDate":"2026-10-30T08:00:00-04:00","endDate":"2026-10-30T13:00:00-04:00","location":{"@type":"Place","name":"Tampa Bay Sporting Clays"}}]</script>'
t, s, e, ad, loc = bc.parse_detail_page(f"<html><head>{ld}</head><body><h1>x</h1></body></html>", "u")
good = t == "SUCA Clay Shoot" and s.hour == 8 and loc == "Tampa Bay Sporting Clays"; ok &= good
print(("PASS" if good else "FAIL"), "JSON-LD |", t, s, loc)

# Recurring: DBIA 3rd Wednesday
evs = bc.src_recurring({"org":"DBIA","title":"Gulf Coast Chapter Members Meeting","weekday":"WE","nth":3,"start":"15:30","end":"17:00","months":3})
days = [e.start_dt() for e in evs]
good = all(d.weekday()==2 and 15<=d.day<=21 and d.hour==15 for d in days); ok &= good
print(("PASS" if good else "FAIL"), "Recurring 3rd Wed |", [d.strftime("%a %b %d %H:%M") for d in days])
sys.exit(0 if ok else 1)

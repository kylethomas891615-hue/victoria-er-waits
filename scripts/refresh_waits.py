#!/usr/bin/env python3
"""Refresh the three Greater Victoria ER waits in index.html from Island Health.

Source: https://www.islandhealth.ca/find-care (the waits are server-rendered in
the HTML; each ED is a `div.emergency-departments` card with an `h2.token-name`
and a `div.wait-time > div.inner`).

Only these elements are ever changed: p#vgh, p#rjh, p#sph, p#vgh-read,
p#rjh-read, p#sph-read, and the header p#updated ("Last updated <read time>"),
which is set to the same read time whenever the page is rewritten. Nothing is guessed: if a hospital's posted wait cannot
be read, its old value and old read time are left as they are.

SPH conventions (same as the existing page):
  * outside 7:00 a.m.-10:00 p.m. PT, or if Island Health says closed -> "Closed"
    (rendered as <p class="wait closed" id="sph">Closed</p>)
  * open but no wait posted (Island Health shows "OPEN") -> "Open - Wait time unavailable"

Exit codes: 0 = ok (file may or may not have changed), 1 = could not read any
hospital (source down or layout changed), 2 = index.html structure not found.

Prints a commit message on stdout line "COMMIT_MSG=..." and "CHANGED=true|false".
"""
import argparse
import datetime as dt
import html as htmllib
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

SOURCE = "https://www.islandhealth.ca/find-care"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
PT = ZoneInfo("America/Vancouver")
WANTED = [  # (Island Health name, element id, commit label)
    ("Victoria General Hospital ED", "vgh", "VGH"),
    ("Royal Jubilee Hospital ED", "rjh", "RJH"),
    ("Saanich Peninsula Hospital ED", "sph", "SPH"),
]
SPH_OPEN_NO_WAIT = "Open - Wait time unavailable"
WAIT_RE = re.compile(r"^(?:(\d{1,2}) hours?)?(?: ?(\d{1,2}) minutes?)?$")


def fetch(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-CA,en;q=0.9",
                "Cache-Control": "no-cache",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # network hiccup; retry
            last = e
    raise RuntimeError(f"fetch failed: {last}")


def text_of(fragment):
    t = re.sub(r"<[^>]+>", " ", fragment)
    t = htmllib.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def parse_source(page):
    """Return {Island Health name: posted text} for every ED card."""
    found = {}
    for card in page.split('class="emergency-departments')[1:]:
        name_m = re.search(r'class="token-name">\s*(?:<a[^>]*>)?([^<]+)', card)
        inner_m = re.search(r'class="inner">\s*(.*?)\s*</div>', card, re.S)
        if name_m and inner_m:
            found[text_of(name_m.group(1))] = text_of(inner_m.group(1))
    return found


def wait_ok(text):
    m = WAIT_RE.match(text or "")
    return bool(text) and bool(m) and (m.group(1) or m.group(2))


def short(text):
    """'5 hours 45 minutes' -> '5h45', '4 hours' -> '4h', 'Closed' -> 'Closed'."""
    m = WAIT_RE.match(text)
    if not m or not (m.group(1) or m.group(2)):
        return {SPH_OPEN_NO_WAIT: "open, wait unavailable"}.get(text, text)
    h = int(m.group(1) or 0)
    mins = m.group(2)
    return f"{h}h{int(mins):02d}" if mins else f"{h}h"


def stamp(now):
    h12 = now.hour % 12 or 12
    ampm = "a.m." if now.hour < 12 else "p.m."
    return f"{h12}:{now.minute:02d} {ampm} PT, {now.strftime('%B')} {now.day}, {now.year}"


def decide(found, now):
    """Map element id -> new display text, only for values we could read."""
    out = {}
    for name, el, _ in WANTED:
        raw = found.get(name)
        if el == "sph":
            minutes = now.hour * 60 + now.minute
            if raw and re.search(r"\bclosed\b", raw, re.I):
                out[el] = "Closed"
            elif minutes < 7 * 60 or minutes >= 22 * 60:
                out[el] = "Closed"  # posted hours: 7 a.m. to 10 p.m. only
            elif raw and wait_ok(raw):
                out[el] = raw
            elif raw and raw.strip().upper() == "OPEN":
                out[el] = SPH_OPEN_NO_WAIT
        else:
            if raw and wait_ok(raw):
                out[el] = raw
    return out


def apply(index, values, read):
    new = index
    for el, text in values.items():
        wait_re = re.compile(r'<p class="wait(?: closed)?" id="%s">[^<]*</p>' % el)
        cls = "wait closed" if text == "Closed" else "wait"
        rep = f'<p class="{cls}" id="{el}">{htmllib.escape(text, quote=False)}</p>'
        new, n1 = wait_re.subn(lambda _m: rep, new, count=1)
        read_re = re.compile(r'<p class="read" id="%s-read">[^<]*</p>' % el)
        new, n2 = read_re.subn(lambda _m: f'<p class="read" id="{el}-read">Read {read}</p>', new, count=1)
        if n1 != 1 or n2 != 1:
            raise SystemExit(2)
    upd_re = re.compile(r'<p class="note" id="updated">[^<]*</p>')
    new, n3 = upd_re.subn(lambda _m: f'<p class="note" id="updated">Last updated {read}</p>', new, count=1)
    if n3 != 1:
        raise SystemExit(2)
    return new


def current(index, el):
    m = re.search(r'<p class="wait(?: closed)?" id="%s">([^<]*)</p>' % el, index)
    r = re.search(r'<p class="read" id="%s-read">Read ([^<]*)</p>' % el, index)
    return (htmllib.unescape(m.group(1)) if m else None, r.group(1) if r else None)


def parse_stamp(s):
    try:
        d = dt.datetime.strptime(s.replace("a.m.", "AM").replace("p.m.", "PM").replace(" PT", ""),
                                 "%I:%M %p, %B %d, %Y")
        return d.replace(tzinfo=PT)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index.html")
    ap.add_argument("--source-file", help="parse a saved copy instead of fetching")
    ap.add_argument("--stamp-max-age", type=int, default=30,
                    help="minutes: if no wait changed, only rewrite read times older than this")
    args = ap.parse_args()

    now = dt.datetime.now(PT)
    try:
        page = open(args.source_file).read() if args.source_file else fetch(SOURCE)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    found = parse_source(page)
    values = decide(found, now)
    for name, el, _ in WANTED:
        print(f"{el}: source={found.get(name)!r} -> {values.get(el, '(unreadable, kept old)')!r}", file=sys.stderr)
    if not values:
        print("ERROR: no Greater Victoria wait could be read; page left unchanged", file=sys.stderr)
        return 1

    index = open(args.index, encoding="utf-8").read()
    olds = {el: current(index, el) for _, el, _ in WANTED}
    if any(v[0] is None or v[1] is None for v in olds.values()):
        print("ERROR: index.html structure not found", file=sys.stderr)
        return 2
    waits_changed = any(olds[el][0] != v for el, v in values.items())
    oldest = [parse_stamp(olds[el][1]) for el in values]
    stale = any(o is None or (now - o).total_seconds() >= args.stamp_max_age * 60 for o in oldest)
    if not waits_changed and not stale:
        print("CHANGED=false")
        return 0

    read = stamp(now)
    new = apply(index, values, read)
    if new == index:
        print("CHANGED=false")
        return 0
    with open(args.index, "w", encoding="utf-8") as f:
        f.write(new)
    parts = []
    for _, el, label in WANTED:
        parts.append(f"{label} {short(values.get(el, olds[el][0]))}")
    print("CHANGED=true")
    print(f"COMMIT_MSG=Refresh ER waits: {', '.join(parts)} ({read})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
import os
import re
import time
from datetime import date

import httpx

HTTP = httpx.Client(timeout=60, follow_redirects=True)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def host_of(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url or "", re.I)
    return m.group(1) if m else ""


def digits_of(phone) -> str:
    return re.sub(r"[^0-9]", "", str(phone or ""))


def phone_key(phone) -> str:
    return re.sub(r"^1", "", digits_of(phone))[-10:]


def norm_key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def business_key(lead: dict) -> str:
    return f"{norm_key(lead.get('Business_Name'))}|{norm_key(lead.get('City'))}|{norm_key(lead.get('State'))}"


# ---------- Serper (Google Places) ----------

SOCIAL_HOST = re.compile(
    r"(^|\.)(yelp\.com|facebook\.com|instagram\.com|bbb\.org|angi\.com|angieslist\.com|homeadvisor\.com|"
    r"yellowpages\.com|mapquest\.com|linktr\.ee|nextdoor\.com|booksy\.com|tripadvisor\.com|google\.com|wa\.me|"
    r"twitter\.com|x\.com|tiktok\.com|fb\.me|t\.me|m\.facebook)",
    re.I,
)


def serper_places(query: str, num: int = 20) -> list[dict]:
    try:
        r = HTTP.post(
            "https://google.serper.dev/places",
            headers={"X-API-KEY": env("SERPER_API_KEY")},
            json={"q": query, "gl": "us", "num": num},
        )
        r.raise_for_status()
        places = r.json().get("places")
        return places if isinstance(places, list) else []
    except (httpx.HTTPError, ValueError):
        return []


def split_places(places: list[dict], niche: str, city: str, us_state: str) -> list[dict]:
    out = []
    for p in places:
        phone = (p.get("phoneNumber") or "").strip()
        if not phone:
            continue
        site = (p.get("website") or "").strip()
        if site and SOCIAL_HOST.search(host_of(site)):
            site = ""
        rc = p.get("ratingCount")
        rc = int(rc) if rc is not None else None
        if rc is not None and rc > 200:
            continue
        out.append({
            "Business_Name": p.get("title") or "",
            "Category": niche,
            "City": city,
            "State": us_state,
            "Phone": phone,
            "Website": site,
            "Rating": p.get("rating") if p.get("rating") is not None else "",
            "Reviews": rc if rc is not None else "",
            "Maps_URL": f"https://www.google.com/maps?cid={p['cid']}" if p.get("cid") else "",
        })
    out.sort(key=lambda b: 1e9 if b["Reviews"] == "" else b["Reviews"])
    with_site = [b for b in out if b["Website"]][:10]
    no_site = [b for b in out if not b["Website"]][:8]
    return no_site + with_site


# ---------- Firecrawl ----------

def _fc_headers() -> dict:
    return {"Authorization": f"Bearer {env('FIRECRAWL_API_KEY')}"}


def firecrawl_scrape(url: str) -> str:
    for _ in range(3):
        try:
            r = HTTP.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers=_fc_headers(),
                json={"url": url, "formats": ["markdown"], "onlyMainContent": False},
                timeout=45,
            )
        except httpx.HTTPError:
            return ""
        if r.status_code == 429:
            m = re.search(r"retry after (\d+)", r.text, re.I)
            time.sleep((int(m.group(1)) if m else 5) + 0.5)
            continue
        if r.is_error:
            return ""
        data = (r.json() or {}).get("data") or {}
        if not data:
            return ""
        return (data.get("markdown") or "") + " " + json.dumps(data.get("metadata") or {})
    return ""


def firecrawl_search(query: str) -> str:
    try:
        r = HTTP.post(
            "https://api.firecrawl.dev/v1/search",
            headers=_fc_headers(),
            json={"query": query, "limit": 2, "scrapeOptions": {"formats": ["markdown"]}},
            timeout=60,
        )
        r.raise_for_status()
        blob = ""
        for d in (r.json() or {}).get("data") or []:
            for k in ("markdown", "description", "title"):
                if d.get(k):
                    blob += " " + d[k]
        return blob
    except (httpx.HTTPError, ValueError):
        return ""


# ---------- Website quality ----------

BUILDER = re.compile(
    r"(wixsite|godaddysites|weebly|business\.site|strikingly|webnode|jimdo|wordpress\.com|yola\.com|webs\.com|"
    r"homestead|mystrikingly|site123|000webhost)",
    re.I,
)
UNDER_CONSTRUCTION = re.compile(
    r"(under construction|coming soon|site is being built|future home of|this domain is parked|"
    r"domain is for sale|website coming)",
    re.I,
)
COPYRIGHT_YEAR = re.compile(r"(?:&copy;|&#169;|©|copyright)[^0-9]{0,14}(20[0-2][0-9])", re.I)
ANY_YEAR = re.compile(r"\b(20[0-2][0-9])\b")


def assess_website(site: str, md: str) -> tuple[str, str]:
    """Returns (quality, issue): quality is 'good' or 'very_outdated'."""
    host = host_of(site)
    c_years = [int(y) for y in COPYRIGHT_YEAR.findall(md)]
    a_years = [int(y) for y in ANY_YEAR.findall(md) if 2005 <= int(y) <= date.today().year]
    max_year = max(c_years) if c_years else (max(a_years) if a_years else 0)
    length = len(re.sub(r"\s+", " ", md))

    reasons = []
    if BUILDER.search(host) or BUILDER.search(md):
        reasons.append("diy_builder")
    if re.match(r"^http://", site, re.I):
        reasons.append("no_https")
    if max_year and max_year <= 2021:
        reasons.append(f"old_copyright_{max_year}")
    if UNDER_CONSTRUCTION.search(md):
        reasons.append("under_construction")
    if 0 < length < 500:
        reasons.append("thin_site")
    if length == 0:
        reasons.append("unreachable_site")
    return ("very_outdated", "|".join(reasons)) if reasons else ("good", "")


# ---------- Emails ----------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
JUNK_EMAIL = re.compile(
    r"(example\.(com|org)|sentry|wixpress\.com|\.png|\.jpg|\.jpeg|\.gif|\.webp|\.svg|@2x|@3x|godaddy\.com|"
    r"yourdomain|yourcompany|domain\.com|email\.(com|here)|placeholder|wixsite|schema\.org|w3\.org|googleapis|"
    r"gstatic|cloudflare|user@|name@|firstname|lastname|john@|jane@|abc@|xyz@|test@|no-?reply|donotreply)",
    re.I,
)
FILE_EXT = re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|css|js)$", re.I)
ROLE_PREFIX = re.compile(
    r"^(info|contact|hello|office|sales|admin|service|support|team|booking|bookings|estimate|estimates|"
    r"quote|quotes|help|reception|owner)"
)
FREEMAIL = re.compile(r"(gmail|yahoo|hotmail|outlook|aol|icloud|live|msn)\.")


def _email_score(e: str) -> int:
    local, _, domain = e.partition("@")
    s = 3 if ROLE_PREFIX.search(local) else 0
    s += 1 if FREEMAIL.search(domain) else 2
    if len(local) > 30:
        s -= 2
    return s


def pick_emails(text: str) -> list[str]:
    found = [re.sub(r"[.,;:]$", "", e.lower()) for e in EMAIL_RE.findall(text)]
    found = [e for e in found if not JUNK_EMAIL.search(e) and len(e) <= 60 and not FILE_EXT.search(e)]
    unique = list(dict.fromkeys(found))
    return sorted(unique, key=_email_score, reverse=True)


def has_mx(domain: str) -> bool:
    try:
        r = HTTP.get("https://dns.google/resolve", params={"name": domain, "type": "MX"}, timeout=15)
        return bool((r.json() or {}).get("Answer"))
    except (httpx.HTTPError, ValueError):
        return False


# ---------- Numverify ----------

QUOTA_EXHAUSTED = "quota_exhausted"


def numverify(digits: str):
    """Returns the Numverify response dict, None on failure, or QUOTA_EXHAUSTED."""
    num = "1" + digits if len(digits) == 10 else digits
    if len(num) < 11:
        return None
    for _ in range(8):
        try:
            r = HTTP.get(
                "https://apilayer.net/api/validate",
                params={"access_key": env("NUMVERIFY_API_KEY"), "number": num, "country_code": "US", "format": 1},
                timeout=20,
            )
        except httpx.HTTPError:
            return None
        if r.status_code == 429:
            time.sleep(4)
            continue
        if r.is_error:
            return None
        body = r.json() or {}
        code = (body.get("error") or {}).get("code")
        if code == 106:  # rate limited
            time.sleep(3.5)
            continue
        if code in (101, 104):  # bad key / monthly quota reached
            return QUOTA_EXHAUSTED
        return body
    return None


# ---------- Gemini ----------

def gemini_verify(lead: dict):
    key = env("GEMINI_API_KEY")
    model = env("GEMINI_MODEL", "gemini-flash-lite-latest")
    prompt = (
        "You verify US local-service business sales leads for a web-design agency. The business either has NO "
        "website or only a weak/outdated one (a web-design agency prospect). Decide if this looks like a REAL, "
        "currently-operating US small business whose given email plausibly belongs to the business owner/office "
        "(not a lead-aggregator, directory, or marketing platform). Respond with ONLY compact JSON: "
        '{"legit":true|false,"confidence":0.0-1.0,"reason":"short"}. Business='
        + json.dumps({
            "name": lead.get("Business_Name"), "city": lead.get("City"), "state": lead.get("State"),
            "category": lead.get("Category"), "email": lead.get("Email"), "phone": lead.get("Phone"),
            "carrier": lead.get("Carrier"), "line_type": lead.get("Line_Type"),
        })
    )
    for _ in range(3):
        try:
            r = HTTP.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0, "maxOutputTokens": 120},
                },
                timeout=25,
            )
        except httpx.HTTPError:
            return None
        if r.status_code in (429, 503):
            time.sleep(4)
            continue
        if r.is_error:
            return None
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"] or ""
        except (KeyError, IndexError, TypeError, ValueError):
            text = ""
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None

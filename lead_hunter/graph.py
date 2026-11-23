import logging
import re
import time
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import services as svc
from . import sheets

log = logging.getLogger("lead_hunter")

BAD_EMAIL_DOMAIN = re.compile(
    r"(birdeye|yext|thumbtack|angi\.|angieslist|porch\.com|networx|homeadvisor|houzz|yelp|bbb\.org|nextdoor|"
    r"mapquest|yellowpages|facebook|instagram|wixpress|godaddy|sentry|example\.|wordpress\.com|squarespace|"
    r"weebly|mailchimp|constantcontact|hubspot|salesforce|godaddysites|business\.site)",
    re.I,
)
RISKY_LINE_TYPES = re.compile(r"(voip|toll_free|premium_rate|special_services|pager|satellite)")


class LeadState(TypedDict, total=False):
    niche: str
    city: str
    us_state: str
    places: list[dict]
    candidates: list[dict]
    scraped: list[dict]
    phone_checked: list[dict]
    verified: list[dict]
    new_leads: list[dict]


def search_places(state: LeadState) -> dict:
    query = f"{state.get('niche', '')} {state.get('city', '')} {state.get('us_state', '')}"
    places = svc.serper_places(query)
    log.info("Serper returned %d places for %r", len(places), query)
    return {"places": places}


def split_places(state: LeadState) -> dict:
    candidates = svc.split_places(state["places"], state.get("niche", ""), state.get("city", ""), state.get("us_state", ""))
    log.info("Split kept %d candidates", len(candidates))
    return {"candidates": candidates}


def scrape_and_extract_email(state: LeadState) -> dict:
    out = []
    for biz in state["candidates"]:
        site = (biz.get("Website") or "").strip()
        if site:
            status = "live"
            md = svc.firecrawl_scrape(site)
            quality, issue = svc.assess_website(site, md)
            emails = svc.pick_emails(md)
        else:
            status, quality, issue = "none", "no_website", "no_website"
            blob = svc.firecrawl_search(f"{biz['Business_Name']} {biz['City']} {biz['State']} contact email")
            emails = svc.pick_emails(blob)

        if quality == "good" or not (biz.get("Phone") or "").strip() or not emails:
            continue
        best = next((e for e in emails[:4] if svc.has_mx(e.split("@")[1])), "")
        if not best:
            continue

        out.append({
            "Date_Added": date.today().isoformat(),
            "Business_Name": biz["Business_Name"], "Category": biz["Category"],
            "City": biz["City"], "State": biz["State"],
            "Email": best, "Email_MX": "valid", "All_Emails": ", ".join(emails[:5]),
            "Phone": biz["Phone"].strip(),
            "Website": site, "Website_Status": status, "Website_Quality": quality, "Website_Issue": issue,
            "Rating": biz["Rating"], "Reviews": biz["Reviews"],
            "Source": "serper_places+site" if site else "serper_places+search",
            "Maps_URL": biz["Maps_URL"],
        })
        time.sleep(0.25)
    log.info("Scrape kept %d leads with an MX-valid email", len(out))
    return {"scraped": out}


def validate_phones(state: LeadState) -> dict:
    out = []
    seen_e, seen_p, seen_b = set(), set(), set()
    quota_hit = False
    for lead in state["scraped"]:
        lead = dict(lead)
        digits = svc.digits_of(lead.get("Phone"))
        if not lead.get("Email") or len(digits) < 10:
            continue
        ek = lead["Email"].lower().strip()
        pk = svc.phone_key(digits)
        bk = svc.business_key(lead)
        if ek in seen_e or (len(pk) == 10 and pk in seen_p) or (bk != "||" and bk in seen_b):
            continue
        seen_e.add(ek)
        if len(pk) == 10:
            seen_p.add(pk)
        seen_b.add(bk)
        if quota_hit:
            continue

        nv = svc.numverify(digits)
        time.sleep(1.7)
        if nv == svc.QUOTA_EXHAUSTED:
            log.warning("Numverify quota exhausted; remaining leads are dropped")
            quota_hit = True
            continue
        if not nv or not nv.get("valid"):
            continue
        if nv.get("country_code") and nv["country_code"] != "US":
            continue
        line_type = (nv.get("line_type") or "").lower()
        if RISKY_LINE_TYPES.search(line_type):
            continue

        lead["Phone_Valid"] = "yes"
        lead["Line_Type"] = nv.get("line_type") or ""
        lead["Carrier"] = nv.get("carrier") or ""
        lead["Voicemail_Risk"] = "low" if line_type == "mobile" else "medium"
        out.append(lead)

    out.sort(key=lambda l: 0 if l.get("Website_Status") == "none" else 1)
    log.info("Phone validation kept %d leads", len(out))
    return {"phone_checked": out}


def ai_verify(state: LeadState) -> dict:
    use_ai = bool(svc.env("GEMINI_API_KEY"))
    out = []
    for lead in state["phone_checked"]:
        lead = dict(lead)
        domain = (lead.get("Email") or "").lower().partition("@")[2]
        if BAD_EMAIL_DOMAIN.search(domain):
            continue
        verified, conf, reason = "yes", "", "rule_based"
        if use_ai:
            v = svc.gemini_verify(lead)
            time.sleep(0.3)
            if v:
                conf = v.get("confidence", "") if v.get("confidence") is not None else ""
                reason = str(v.get("reason") or "")[:120]
                if v.get("legit") is False and (v.get("confidence") is None or v["confidence"] >= 0.6):
                    continue
                verified = "no" if v.get("legit") is False else "yes"
            else:
                reason = "ai_unavailable_passed"
        lead.update(Verified=verified, Verify_Confidence=conf, Verify_Reason=reason)
        out.append(lead)
    log.info("AI verification kept %d leads", len(out))
    return {"verified": out}


def dedupe_against_sheet(state: LeadState) -> dict:
    existing = sheets.read_rows()
    emails, phones, businesses = set(), set(), set()
    for row in existing:
        if e := str(row.get("Email") or "").lower().strip():
            emails.add(e)
        if len(p := svc.phone_key(row.get("Phone"))) == 10:
            phones.add(p)
        if (b := svc.business_key(row)) != "||":
            businesses.add(b)

    out = []
    for lead in state["verified"]:
        e = str(lead.get("Email") or "").lower().strip()
        p = svc.phone_key(lead.get("Phone"))
        b = svc.business_key(lead)
        if (e and e in emails) or (len(p) == 10 and p in phones) or (b != "||" and b in businesses):
            continue
        if e:
            emails.add(e)
        if len(p) == 10:
            phones.add(p)
        if b != "||":
            businesses.add(b)
        out.append(lead)
    log.info("Dedupe against %d existing rows kept %d new leads", len(existing), len(out))
    return {"new_leads": out}


def write_sheet(state: LeadState) -> dict:
    sheets.append_leads(state["new_leads"])
    log.info("Wrote %d leads to the sheet", len(state["new_leads"]))
    return {}


def _continue_if(key: str, next_node: str):
    return lambda state: next_node if state.get(key) else END


def build_graph():
    g = StateGraph(LeadState)
    g.add_node("search_places", search_places)
    g.add_node("split_places", split_places)
    g.add_node("scrape_and_extract_email", scrape_and_extract_email)
    g.add_node("validate_phones", validate_phones)
    g.add_node("ai_verify", ai_verify)
    g.add_node("dedupe_against_sheet", dedupe_against_sheet)
    g.add_node("write_sheet", write_sheet)

    g.add_edge(START, "search_places")
    g.add_edge("search_places", "split_places")
    steps = [
        ("split_places", "candidates", "scrape_and_extract_email"),
        ("scrape_and_extract_email", "scraped", "validate_phones"),
        ("validate_phones", "phone_checked", "ai_verify"),
        ("ai_verify", "verified", "dedupe_against_sheet"),
        ("dedupe_against_sheet", "new_leads", "write_sheet"),
    ]
    for node, key, next_node in steps:
        g.add_conditional_edges(node, _continue_if(key, next_node), [next_node, END])
    g.add_edge("write_sheet", END)
    return g.compile()


def stream_lead_hunter(niche: str, city: str, us_state: str):
    """Yields (node_name, state_update) as each step of the graph finishes."""
    for chunk in build_graph().stream({"niche": niche, "city": city, "us_state": us_state}, stream_mode="updates"):
        for node, update in chunk.items():
            yield node, update or {}


def run_lead_hunter(niche: str, city: str, us_state: str) -> list[dict]:
    final = build_graph().invoke({"niche": niche, "city": city, "us_state": us_state})
    return final.get("new_leads", [])


def count_leads() -> dict:
    rows = sheets.read_rows()
    emails = list(dict.fromkeys(str(r.get("Email") or "").lower() for r in rows if r.get("Email")))
    return {"count": len(emails), "nowebsite": len(emails), "rows": len(rows), "emails": emails,
            "sheet_url": sheets.sheet_url()}

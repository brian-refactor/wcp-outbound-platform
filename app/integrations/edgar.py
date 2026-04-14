"""
SEC EDGAR Form D integration.

Searches public Form D filings (private placement disclosures) to surface
accredited investor contacts — fund principals, family office operators, etc.
"""
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"

# EDGAR requires a descriptive User-Agent per their access policy
HEADERS = {
    "User-Agent": "WCP Outbound Platform ops@willowcreekpartners.com",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"),
    ("KY", "Kentucky"), ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"),
    ("OR", "Oregon"), ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"),
    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"),
    ("VT", "Vermont"), ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
]

INDUSTRY_GROUPS = [
    "Pooled Investment Fund",
    "Real Estate",
    "Banking & Financial Services",
    "Business Services",
    "Technology",
    "Healthcare & Life Sciences",
    "Energy",
    "Other",
]


def search_form_d(
    keywords: str = "",
    state: str = "",
    start_date: str = "",
    end_date: str = "",
    offset: int = 0,
    size: int = 20,
) -> tuple[list[dict], int]:
    """
    Search EDGAR for Form D filings.
    Returns (list of filing stubs, total_count).
    Each stub has: entity_name, file_date, biz_location, accession_no, cik
    """
    params: dict = {"forms": "D", "from": offset, "size": size}

    if keywords:
        params["q"] = keywords
    if state:
        params["locationCode"] = state
    if start_date or end_date:
        params["dateRange"] = "custom"
    if start_date:
        params["startdt"] = start_date
    if end_date:
        params["enddt"] = end_date

    try:
        with httpx.Client(headers=HEADERS, timeout=15.0) as client:
            resp = client.get(EDGAR_SEARCH_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error("EDGAR search failed: %s", e)
        return [], 0

    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)

    results = []
    for hit in hits.get("hits", []):
        src = hit.get("_source", {})
        accession_no = src.get("adsh", "")
        raw_cik = (src.get("ciks") or [""])[0]
        cik = str(int(raw_cik)) if raw_cik else ""
        # display_names format: "ENTITY NAME  (TICKER)  (CIK 0001234567)"
        display_name = (src.get("display_names") or [""])[0]
        entity_name = display_name.split("  (")[0].strip() if display_name else ""
        biz_location = (src.get("biz_locations") or [""])[0]
        results.append({
            "entity_name": entity_name,
            "file_date": src.get("file_date", ""),
            "biz_location": biz_location,
            "accession_no": accession_no,
            "cik": cik,
        })
    return results, total


def enrich_filings(filings: list[dict]) -> list[dict]:
    """
    Fetch Form D XML for each filing in parallel and merge in detail fields.
    Returns a flat list of contact rows (one row per related person per filing).
    """
    enriched = []

    def _fetch(filing: dict) -> Optional[dict]:
        detail = fetch_filing_detail(filing["cik"], filing["accession_no"])
        if detail:
            return {**filing, **detail}
        return {**filing, "related_persons": [], "industry": "", "total_offering": 0, "date_of_first_sale": ""}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch, f): f for f in filings}
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                enriched.append(result)

    # Sort back to original order by file_date desc
    enriched.sort(key=lambda x: x.get("file_date", ""), reverse=True)

    # Flatten to one row per contact person, deduplicating as we go
    rows = []
    seen: set[tuple] = set()  # (normalized_name, normalized_entity)

    for filing in enriched:
        persons = filing.get("related_persons") or []
        for person in persons:
            name = person.get("name", "").strip()
            if not name or _is_entity_name(name):
                continue
            entity = (filing.get("entity_name") or "").strip()
            key = (name.lower(), entity.lower())
            if key in seen:
                continue
            seen.add(key)
            rows.append({**filing, **person})

    return rows


# Suffixes that indicate a related-person entry is an organization, not an individual
_ENTITY_SUFFIXES = (
    " llc", " lp", " l.p.", " ltd", " inc", " corp", " co.", " fund",
    " trust", " partners", " group", " family office", " management",
    " capital", " investments", " associates", " advisors", " advisers",
)

def _is_entity_name(name: str) -> bool:
    """Return True if the name looks like a company rather than a person."""
    lower = name.lower()
    if lower.startswith("n/a"):
        return True
    return any(lower.endswith(suffix) or f"{suffix} " in lower for suffix in _ENTITY_SUFFIXES)


def fetch_filing_detail(cik: str, accession_no: str) -> Optional[dict]:
    """
    Download and parse a Form D XML for a single filing.
    Returns dict with industry, total_offering, date_of_first_sale, related_persons.
    """
    if not cik or not accession_no:
        return None

    accession_nodash = accession_no.replace("-", "")
    url = f"{EDGAR_ARCHIVES_URL}/{cik}/{accession_nodash}/primary_doc.xml"

    try:
        with httpx.Client(headers=HEADERS, timeout=10.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return _parse_form_d_xml(resp.text)
    except Exception as e:
        logger.debug("Failed to fetch Form D XML %s: %s", url, e)
        return None


def _cik_from_accession(accession_no: str) -> str:
    """Extract numeric CIK from accession number prefix (strip leading zeros)."""
    parts = accession_no.split("-")
    if parts:
        try:
            return str(int(parts[0]))
        except ValueError:
            pass
    return ""


def _child_text(element, tag_name: str) -> str:
    """Find a direct or nested child by local tag name and return its text."""
    for child in element.iter():
        if child.tag.split("}")[-1] == tag_name and child.text:
            return child.text.strip()
    return ""


def _parse_form_d_xml(xml_text: str) -> dict:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("XML parse error: %s", e)
        return {}

    # Entity name
    entity_name = _child_text(root, "entityName")

    # State from issuer address
    state = ""
    for el in root.iter():
        if el.tag.split("}")[-1] == "issuerAddress":
            state = _child_text(el, "stateOrCountry")
            break

    # Industry
    industry = _child_text(root, "industryGroupType")
    fund_type = _child_text(root, "investmentFundType")
    if fund_type:
        industry = f"{industry} — {fund_type}" if industry else fund_type

    # Offering amount
    total_offering_raw = _child_text(root, "totalOfferingAmount")
    try:
        total_offering = int(float(total_offering_raw)) if total_offering_raw else 0
    except (ValueError, TypeError):
        total_offering = 0

    # Date of first sale
    date_of_first_sale = _child_text(root, "dateOfFirstSale")

    # Issuer phone
    issuer_phone = _child_text(root, "issuerPhoneNumber")

    # Offering details
    total_sold_raw = _child_text(root, "totalAmountSold")
    try:
        total_sold = int(float(total_sold_raw)) if total_sold_raw else 0
    except (ValueError, TypeError):
        total_sold = 0

    min_investment_raw = _child_text(root, "minimumInvestmentAccepted")
    try:
        min_investment = int(float(min_investment_raw)) if min_investment_raw else 0
    except (ValueError, TypeError):
        min_investment = 0

    num_investors_raw = _child_text(root, "totalNumberAlreadyInvested")
    try:
        num_investors = int(num_investors_raw) if num_investors_raw else 0
    except (ValueError, TypeError):
        num_investors = 0

    # Related persons
    related_persons = []
    for el in root.iter():
        if el.tag.split("}")[-1] != "relatedPersonInfo":
            continue
        first = last = title = person_city = person_state = ""
        for child in el:
            local = child.tag.split("}")[-1]
            if local == "relatedPersonName":
                first = _child_text(child, "firstName")
                last = _child_text(child, "lastName")
            elif local == "relatedPersonTitle":
                title = _child_text(child, "officerTitle")
            elif local == "relatedPersonAddress":
                person_city = _child_text(child, "city")
                person_state = _child_text(child, "stateOrCountry")
        name = f"{first} {last}".strip()
        if name:
            related_persons.append({
                "name": name,
                "title": title,
                "person_city": person_city,
                "person_state": person_state,
            })

    return {
        "entity_name": entity_name,
        "state": state,
        "industry": industry,
        "total_offering": total_offering,
        "total_sold": total_sold,
        "min_investment": min_investment,
        "num_investors": num_investors,
        "issuer_phone": issuer_phone,
        "date_of_first_sale": date_of_first_sale,
        "related_persons": related_persons,
    }

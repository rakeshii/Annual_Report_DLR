import io
import re
import sys
import time
import platform
import zipfile
import requests
import streamlit as st
from datetime import datetime

# ── Windows Python 3.13 fix (local dev only) ──────────────────
if platform.system() == "Windows":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Annual Report Downloader", page_icon="📄", layout="wide")

st.markdown("""
<style>
.log-box {
    background:#1e1e2e; color:#cdd6f4;
    font-family:'Courier New',monospace; font-size:0.82rem;
    border-radius:8px; padding:1rem 1.2rem;
    max-height:380px; overflow-y:auto;
    white-space:pre-wrap; line-height:1.6;
}
.pill-bse { background:#3498db; color:white; padding:2px 9px; border-radius:12px; font-size:0.75rem; font-weight:600; }
.pill-nse { background:#e67e22; color:white; padding:2px 9px; border-radius:12px; font-size:0.75rem; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BSE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
}

NSE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.nseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

TIMEOUT = 30   # seconds — simple requests, no browser needed


# ══════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════
def log(msg: str, logs: list):
    ts = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{ts}] {msg}")


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def fetch_json(url: str, headers: dict, session: requests.Session = None) -> dict | list | None:
    try:
        getter = session.get if session else requests.get
        r = getter(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


def fetch_bytes(url: str, headers: dict) -> bytes | None:
    try:
        r = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
        r.raise_for_status()
        if len(r.content) < 1000:          # suspiciously small = error page
            return None
        return r.content
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# BSE  — pure API, zero browser
#
# Flow:
#   1. Search company name  → get scrip code
#   2. Fetch annual reports → get PDF URL for target year
#   3. Download PDF bytes
#
# Endpoints (all public, no auth):
#   Search : GET https://api.bseindia.com/BseIndiaAPI/api/fetchcomp/w?search={query}
#   Reports: GET https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode={code}
# ══════════════════════════════════════════════════════════════
def _bse_extract_code_name(item: dict, query: str) -> tuple[str, str] | tuple[None, None]:
    code = str(
        item.get("SCRIP_CD")      or item.get("SECURITY_CODE") or
        item.get("scripcode")     or item.get("Scrip_Code")    or
        item.get("scrip_cd")      or item.get("Scripcode")     or
        item.get("ScripCode")     or ""
    ).strip()
    name = (
        item.get("Scrip_Name")    or item.get("SECURITY_NAME") or
        item.get("Issuer_Name")   or item.get("long_name")     or
        item.get("SCRIP_NAME")    or item.get("scrip_name")    or
        item.get("ScripName")     or query
    )
    return (code, str(name)) if code else (None, None)


# ── BSE Equity Master (cached per session) ───────────────────
# BSE publishes a full CSV of all listed equities — no auth needed.
# We download it once, parse into a lookup dict, reuse for all searches.
_BSE_MASTER: dict | None = None   # {name_lower: (code, name), isin: (code, name)}

def _load_bse_master(logs: list) -> dict:
    global _BSE_MASTER
    if _BSE_MASTER is not None:
        return _BSE_MASTER

    log("[BSE] Loading equity master list...", logs)
    _BSE_MASTER = {}

    # BSE equity master CSV — publicly available, updated daily
    urls = [
        "https://www.bseindia.com/corporates/List_Scrips.aspx",   # HTML but has data
        "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=equity&status=Active",
    ]

    # Primary: direct CSV download
    csv_url = "https://www.bseindia.com/downloads/BseIndiaAPI/ListofScrips.csv"
    try:
        r = requests.get(csv_url, headers=BSE_HEADERS, timeout=30)
        if r.status_code == 200 and "," in r.text[:100]:
            import csv, io
            reader = csv.DictReader(io.StringIO(r.text))
            for row in reader:
                code = str(row.get("Security Code") or row.get("Scrip Code") or "").strip()
                name = str(row.get("Security Name") or row.get("Scrip Name") or "").strip()
                isin = str(row.get("ISIN No") or row.get("ISIN") or "").strip()
                if code and name:
                    _BSE_MASTER[name.lower()] = (code, name)
                    if isin:
                        _BSE_MASTER[isin.upper()] = (code, name)
            log(f"[BSE] Master loaded: {len(_BSE_MASTER)} entries from CSV", logs)
            return _BSE_MASTER
    except Exception as e:
        log(f"[BSE-DEBUG] CSV load failed: {e}", logs)

    # Fallback: JSON API
    try:
        session = bse_make_session()
        data = fetch_json(
            "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?segment=equity&status=Active",
            BSE_HEADERS, session
        )
        if isinstance(data, list) and data:
            log(f"[BSE-DEBUG] Master JSON keys: {list(data[0].keys())}", logs)
            for item in data:
                code, name = _bse_extract_code_name(item, "")
                isin = str(
                    item.get("ISIN_NUMBER") or item.get("isin_code") or
                    item.get("ISIN_CODE")   or item.get("isin")      or ""
                ).strip()
                if code and name:
                    _BSE_MASTER[name.lower()] = (code, name)
                    if isin:
                        _BSE_MASTER[isin.upper()] = (code, name)
            log(f"[BSE] Master loaded: {len(_BSE_MASTER)} entries from JSON", logs)
    except Exception as e:
        log(f"[BSE-DEBUG] JSON master failed: {e}", logs)

    return _BSE_MASTER


def _search_bse_master(query: str, logs: list) -> tuple[str, str] | tuple[None, None]:
    """Fuzzy search BSE master list by name or ISIN."""
    master = _load_bse_master(logs)
    if not master:
        return None, None

    q = query.strip().upper()

    # Exact ISIN match
    if q in master:
        return master[q]

    # Exact name match (case-insensitive)
    q_lower = query.strip().lower()
    if q_lower in master:
        return master[q_lower]

    # Strip common suffixes and try again
    stripped = re.sub(
        r"(ltd\.?|limited|inc\.?|corp\.?|pvt\.?|llp|industries|company|enterprises|solutions)",
        "", q_lower, flags=re.I
    ).strip()

    # Substring match — find all names containing the query
    matches = [
        (k, v) for k, v in master.items()
        if len(k) > 4 and (stripped in k or q_lower in k)
        and not k.startswith("IN")   # skip ISIN keys
    ]
    if matches:
        # Prefer shorter names (more exact match)
        matches.sort(key=lambda x: len(x[0]))
        best_k, best_v = matches[0]
        log(f"[BSE] Master match: '{best_k}' → {best_v}", logs)
        return best_v

    # Word-by-word: any key that starts with the first word of query
    first_word = (stripped or q_lower).split()[0]
    if len(first_word) >= 3:
        starts = [
            (k, v) for k, v in master.items()
            if k.startswith(first_word) and not k.startswith("IN")
        ]
        if starts:
            starts.sort(key=lambda x: len(x[0]))
            best_k, best_v = starts[0]
            log(f"[BSE] Master prefix match: '{best_k}' → {best_v}", logs)
            return best_v

    return None, None


def bse_search_company(query: str, logs: list) -> tuple[str, str] | tuple[None, None]:
    """
    Returns (scrip_code, company_name).
    Strategy:
      1. Direct 6-digit code or BSE URL          — instant
      2. NSE cross-lookup via ISIN               — works reliably
      3. BSE equity master CSV/JSON fuzzy search — offline, no API needed
    """
    # ── Strategy 1: direct ────────────────────────────────────────
    if "bseindia.com" in query:
        m = re.search(r"/(\d{6})/", query)
        if m:
            return m.group(1), query
    if re.fullmatch(r"\d{6}", query.strip()):
        return query.strip(), query.strip()

    log(f"[BSE] Searching: '{query}'", logs)

    # ── Strategy 2: NSE → ISIN → BSE master lookup ────────────────
    # NSE search returns ISIN; we use that to find BSE scrip code
    try:
        keyword     = query.split()[0]
        nse_session = nse_make_session()
        nse_url     = f"https://www.nseindia.com/api/search/autocomplete?q={requests.utils.quote(keyword)}"
        nse_data    = fetch_json(nse_url, NSE_HEADERS, nse_session)
        nse_results = (
            nse_data if isinstance(nse_data, list)
            else (nse_data.get("symbols") or nse_data.get("data") or [])
            if isinstance(nse_data, dict) else []
        )

        # Pre-load BSE master ONCE before looping NSE results
        # so ISIN lookup works on first hit
        _load_bse_master(logs)

        for hit in nse_results[:5]:
            symbol = hit.get("symbol") or ""
            if not symbol:
                continue
            info_url = f"https://www.nseindia.com/api/quote-equity?symbol={requests.utils.quote(symbol)}"
            info     = fetch_json(info_url, NSE_HEADERS, nse_session)
            if not isinstance(info, dict):
                continue
            isin = (
                info.get("metadata", {}).get("isin") or
                info.get("info", {}).get("isin")     or
                info.get("securityInfo", {}).get("isin") or ""
            )
            co_name = (
                info.get("info", {}).get("companyName") or
                info.get("metadata", {}).get("companyName") or symbol
            )
            log(f"[BSE] NSE hit: {co_name} | ISIN: {isin}", logs)
            if isin and _BSE_MASTER:
                # Direct ISIN lookup — exact match, no fuzzy
                result = _BSE_MASTER.get(isin.upper())
                if result:
                    code, name = result
                    log(f"[BSE] ✅ Resolved via ISIN {isin}: {name} ({code})", logs)
                    return code, name
    except Exception as e:
        log(f"[BSE-DEBUG] NSE cross-lookup error: {e}", logs)

    # ── Strategy 3: BSE master fuzzy search ───────────────────────
    code, name = _search_bse_master(query, logs)
    if code:
        log(f"[BSE] Found via master: {name} ({code})", logs)
        return code, name

    log(f"[BSE] ❌ Not found.", logs)
    log(f"[BSE] 💡 Enter the 6-digit scrip code directly (BEL = 500049)", logs)
    return None, None


def bse_get_report_url(scrip_code: str, year: int, logs: list, session=None) -> str | None:
    """Fetch annual report list and return PDF URL for target year."""
    if session is None:
        session = bse_make_session()

    log(f"[BSE] Fetching report list for scrip {scrip_code}...", logs)

    endpoints = [
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode={scrip_code}",
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnRptList/w?scripcode={scrip_code}&type=C",
        f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReportNew/w?scripcode={scrip_code}",
    ]

    data = None
    for url in endpoints:
        data = fetch_json(url, BSE_HEADERS, session)
        if isinstance(data, list) and data:
            log(f"[BSE-DEBUG] Report endpoint worked: {url.split('api/')[-1]}", logs)
            break
        elif isinstance(data, dict):
            # Unwrap nested structure
            data = (data.get("Table") or data.get("data") or
                    data.get("AnnualReportList") or data.get("reports") or [])
            if data:
                log(f"[BSE-DEBUG] Unwrapped dict from endpoint", logs)
                break
        data = None

    if not data:
        log(f"[BSE] ❌ No report list returned for {scrip_code}", logs)
        log(f"[BSE] 💡 Verify scrip code at bseindia.com/stock-share-price/.../XXXXXX/", logs)
        return None

    # Show available entries for debug
    if data:
        log(f"[BSE-DEBUG] Report keys: {list(data[0].keys())}", logs)
        # Show actual year values using confirmed field name 'year'
        all_vals = [
            str(item.get("year") or item.get("PERIOD") or item.get("YEAR") or
                item.get("TO_PERIOD") or item.get("toDate") or "?")
            for item in data[:8]
        ]
        log(f"[BSE-DEBUG] Periods found: {all_vals}", logs)

    target_str   = str(year)
    target_short = target_str[-2:]      # "24" for 2024

    for item in data:
        # Read period using confirmed field 'year' first, then fallbacks
        period = str(
            item.get("year")      or item.get("PERIOD")    or
            item.get("YEAR")      or item.get("TO_PERIOD") or
            item.get("toDate")    or item.get("FromDate")  or ""
        )
        # Also scan all values as safety net
        all_text = period + " " + " ".join(str(v) for v in item.values())

        if target_str not in all_text and f"-{target_short}" not in all_text:
            continue

        # Extract PDF using confirmed field 'file_name' first
        pdf = str(
            item.get("file_name")       or item.get("FILENAME")      or
            item.get("fileName")        or item.get("PDFNAME")       or
            item.get("DOCUMENT_NAME")   or item.get("PDF_LINK")      or
            item.get("ATTACHMENTNAME")  or item.get("FILECODE")      or
            item.get("FileNm")          or item.get("STRFILEPATH")   or
            item.get("FILEPATH")        or item.get("FileName")      or ""
        ).strip()

        # Strip accidental double extension (.pdf.pdf)
        if pdf.endswith(".pdf.pdf"):
            pdf = pdf[:-4]

        log(f"[BSE] Year '{period}' matched. pdf='{pdf[:80]}'", logs)

        if not pdf:
            log(f"[BSE-DEBUG] No PDF field. Item: {dict(list(item.items()))}", logs)
            continue

        if pdf.startswith("http"):
            return pdf

        # BSE GUID-style filenames (uuid.pdf) go to /AttachHis/
        # Older named files may be in /AnnualReports/
        is_guid = bool(re.match(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            pdf, re.I
        ))
        bases = (
            ["https://www.bseindia.com/xml-data/corpfiling/AttachHis/"]
            if is_guid else [
                "https://www.bseindia.com/xml-data/corpfiling/AttachHis/",
                "https://www.bseindia.com/AnnualReports/",
                "https://www.bseindia.com/bseplus/AnnualReport/",
            ]
        )
        for base in bases:
            candidate = base + pdf
            try:
                hr = requests.head(candidate, headers=BSE_HEADERS, timeout=8, allow_redirects=True)
                if hr.status_code < 400:
                    log(f"[BSE] ✅ URL confirmed: {candidate}", logs)
                    return candidate
            except Exception:
                pass

        # Return best guess if HEAD checks fail (firewalls sometimes block HEAD)
        best = bases[0] + pdf
        log(f"[BSE] Returning best-guess URL: {best}", logs)
        return best

    log(f"[BSE] ❌ No report matched year {year} in {len(data)} entries", logs)
    return None


def handle_bse(company: str, year: int, logs: list) -> dict | None:
    code, name = bse_search_company(company, logs)
    if not code:
        return None

    # Reuse a fresh session with cookies for report fetch too
    session = bse_make_session()
    pdf_url = bse_get_report_url(code, year, logs, session)
    if not pdf_url:
        return None

    log(f"[BSE] Downloading PDF...", logs)
    data = fetch_bytes(pdf_url, BSE_HEADERS)
    if not data:
        log(f"[BSE] ❌ PDF download failed: {pdf_url}", logs)
        return None

    fname = sanitize(f"BSE_{name}_{year}_AnnualReport.pdf")
    log(f"[BSE] ✅ {fname} ({len(data)/1048576:.2f} MB)", logs)
    return {"filename": fname, "data": data, "exchange": "BSE", "company": name}


# ══════════════════════════════════════════════════════════════
# NSE  — pure API, zero browser
#
# Flow:
#   1. Search company → get symbol
#   2. Fetch annual report list → get PDF URL for target year
#   3. Download PDF bytes
#
# Endpoints:
#   Search : GET https://www.nseindia.com/api/search/autocomplete?q={query}
#   Reports: GET https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}
#
# NSE requires session cookies from the homepage first.
# ══════════════════════════════════════════════════════════════
def nse_make_session() -> requests.Session:
    """Hit NSE homepage to get required cookies, return primed session."""
    s = requests.Session()
    try:
        s.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=TIMEOUT)
    except Exception:
        pass
    return s


def bse_make_session() -> requests.Session:
    """Hit BSE homepage first — BSE API returns empty without session cookies."""
    s = requests.Session()
    try:
        s.get("https://www.bseindia.com", headers=BSE_HEADERS, timeout=TIMEOUT)
        # Also hit the quotes page — this sets additional required cookies
        s.get("https://www.bseindia.com/markets/equity/EQReports/MarketWatch.aspx",
              headers=BSE_HEADERS, timeout=TIMEOUT)
    except Exception:
        pass
    return s


def nse_search_company(query: str, session: requests.Session, logs: list) -> tuple[str, str] | tuple[None, None]:
    log(f"[NSE] Searching: '{query}'", logs)
    url  = f"https://www.nseindia.com/api/search/autocomplete?q={requests.utils.quote(query)}"
    data = fetch_json(url, NSE_HEADERS, session)

    results = []
    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        results = data.get("symbols") or data.get("data") or []

    if results:
        first  = results[0]
        symbol = first.get("symbol") or first.get("data") or ""
        name   = first.get("company_name") or first.get("companyName") or symbol
        if symbol:
            log(f"[NSE] Found: {name} ({symbol})", logs)
            return symbol.upper(), name

    log(f"[NSE] ❌ Company not found: '{query}'", logs)
    return None, None


def nse_get_report_url(symbol: str, year: int, session: requests.Session, logs: list) -> str | None:
    log(f"[NSE] Fetching report list for {symbol}...", logs)

    # Primary endpoint
    url  = f"https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}"
    data = fetch_json(url, NSE_HEADERS, session)

    # Alternate endpoint
    if not data:
        url2 = f"https://www.nseindia.com/api/annual-reports?index=equities&symbol={symbol}&category=annual-report"
        data = fetch_json(url2, NSE_HEADERS, session)

    if not isinstance(data, list) and isinstance(data, dict):
        log(f"[NSE-DEBUG] Response dict keys: {list(data.keys())}", logs)
        data = data.get("data") or data.get("reports") or data.get("annualReports") or []

    if not isinstance(data, list) or not data:
        log(f"[NSE] ❌ No report list returned (got {type(data).__name__})", logs)
        return None

    # 🔍 DEBUG: show field names and available periods
    log(f"[NSE-DEBUG] Report list keys: {list(data[0].keys())}", logs)
    all_periods = [
        f"fromYr={item.get('fromYr','?')} toYr={item.get('toYr','?')}"
        for item in data[:10]
    ]
    log(f"[NSE-DEBUG] Available periods (first 10): {all_periods}", logs)

    # NSE uses fromYr / toYr integer fields.
    # User enters "2024" meaning FY 2023-24 → toYr == 2024
    for item in data:
        from_yr  = str(item.get("fromYr") or "")
        to_yr    = str(item.get("toYr")   or "")
        pdf_url  = str(item.get("fileName") or "")

        log(f"[NSE-DEBUG] Entry: fromYr={from_yr} toYr={to_yr} file={pdf_url[:60]}", logs)

        if to_yr == str(year) or from_yr == str(year):
            log(f"[NSE] ✅ Matched: fromYr={from_yr} toYr={to_yr}", logs)
            if pdf_url:
                full = pdf_url if pdf_url.startswith("http") else f"https://www.nseindia.com{pdf_url}"
                return full
            else:
                log(f"[NSE-DEBUG] Matched but fileName empty. Item: {item}", logs)

    log(f"[NSE] ❌ No report found for toYr={year} (checked {len(data)} entries)", logs)
    return None


def handle_nse(company: str, year: int, logs: list) -> dict | None:
    session        = nse_make_session()
    symbol, name   = nse_search_company(company, session, logs)
    if not symbol:
        return None

    pdf_url = nse_get_report_url(symbol, year, session, logs)
    if not pdf_url:
        return None

    log(f"[NSE] Downloading PDF...", logs)
    data = fetch_bytes(pdf_url, NSE_HEADERS)
    if not data:
        log(f"[NSE] ❌ PDF download failed: {pdf_url}", logs)
        return None

    fname = sanitize(f"NSE_{name}_{year}_AnnualReport.pdf")
    log(f"[NSE] ✅ {fname} ({len(data)/1048576:.2f} MB)", logs)
    return {"filename": fname, "data": data, "exchange": "NSE", "company": name}


# ══════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════
def run_downloads(companies: list, year: int, exchange: str, logs: list) -> list:
    results = []
    for company in companies:
        log(f"\n{'─'*48}", logs)
        log(f"▶ {company}", logs)
        if exchange in ("BSE", "BOTH"):
            res = handle_bse(company, year, logs)
            if res:
                results.append(res)
        if exchange in ("NSE", "BOTH"):
            res = handle_nse(company, year, logs)
            if res:
                results.append(res)
    log(f"\n{'─'*48}", logs)
    log(f"Done — {len(results)} file(s) ready.", logs)
    return results


# ══════════════════════════════════════════════════════════════
# SESSION STATE
# ══════════════════════════════════════════════════════════════
for _k, _v in [("results", []), ("logs", []), ("ran", False)]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ══════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════
st.title("📄 Annual Report Downloader")
st.caption("BSE & NSE · No browser · No scraping · Pure API")
st.divider()

col_l, col_r = st.columns([3, 1], gap="large")

with col_l:
    batch = st.toggle("Batch mode — multiple companies", value=False)
    if batch:
        raw = st.text_area(
            "One company per line (or comma-separated)",
            value="Reliance Industries\nHCL Technologies\nInfosys",
            height=130,
        )
        companies = [c.strip() for line in raw.splitlines() for c in line.split(",") if c.strip()]
        st.caption(f"🏢 {len(companies)} company/companies")
    else:
        single    = st.text_input("Company name or 6-digit BSE scrip code", value="Reliance Industries")
        companies = [single.strip()] if single.strip() else []

with col_r:
    year = st.number_input(
        "Target Year", min_value=2000,
        max_value=datetime.now().year, value=2024, step=1,
    )
    st.caption("BSE → FY ending this year\nNSE → FY starting this year")
    exchange = st.radio("Exchange", ["BSE", "NSE", "BOTH"], horizontal=True)

st.divider()

if st.button("🚀 Fetch Reports", type="primary",
             disabled=not companies, use_container_width=True):
    st.session_state.update(results=[], logs=[], ran=True)
    logs: list = []
    with st.spinner("Fetching via API — usually done in under 30 seconds..."):
        results = run_downloads(companies, year, exchange, logs)
    st.session_state.results = results
    st.session_state.logs    = logs


# ══════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════
if st.session_state.ran:
    st.divider()
    results = st.session_state.results
    logs    = st.session_state.logs

    c1, c2, c3 = st.columns(3)
    c1.metric("Queued", len(companies))
    c2.metric("Downloaded", len(results))
    c3.metric("Exchanges", len({r["exchange"] for r in results}) if results else 0)

    if results:
        st.subheader("📥 Your Files")
        for r in results:
            color = "#3498db" if r["exchange"] == "BSE" else "#e67e22"
            ca, cb = st.columns([4, 1])
            with ca:
                st.markdown(
                    f'<span style="background:{color};color:white;padding:2px 9px;'
                    f'border-radius:12px;font-size:0.75rem;font-weight:600;">'
                    f'{r["exchange"]}</span>&nbsp; **{r["company"]}** '
                    f'<span style="color:#888;font-size:0.8rem;">'
                    f'— {len(r["data"])/1048576:.2f} MB</span>',
                    unsafe_allow_html=True,
                )
            with cb:
                st.download_button(
                    "⬇ Download", data=r["data"],
                    file_name=r["filename"], mime="application/pdf",
                    key=f"dl_{r['filename']}",
                )

        if len(results) > 1:
            st.markdown("---")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for r in results:
                    zf.writestr(r["filename"], r["data"])
            buf.seek(0)
            st.download_button(
                "📦 Download All as ZIP", data=buf,
                file_name=f"AnnualReports_{year}.zip",
                mime="application/zip",
                use_container_width=True,
            )
    else:
        st.warning("No files fetched. Check the log below — the API field names may need tuning for your company.")
        st.info(
            "💡 **Tip:** BSE/NSE occasionally rename their API response fields. "
            "Share the log output and I can adjust the field mappings instantly.",
            icon="ℹ️"
        )

    st.subheader("🖥 Activity Log")
    st.markdown(
        f'<div class="log-box">{"<br>".join(logs)}</div>',
        unsafe_allow_html=True,
    )

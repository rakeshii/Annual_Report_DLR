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
def bse_search_company(query: str, logs: list) -> tuple[str, str] | tuple[None, None]:
    """Returns (scrip_code, company_name) or (None, None)."""
    # If user pasted a BSE URL, extract code directly
    if "bseindia.com" in query:
        m = re.search(r'/(\d{6})/', query)
        if m:
            return m.group(1), query
    # If raw 6-digit code
    if query.strip().isdigit() and len(query.strip()) == 6:
        return query.strip(), query.strip()

    log(f"[BSE] Searching: '{query}'", logs)
    url  = f"https://api.bseindia.com/BseIndiaAPI/api/fetchcomp/w?search={requests.utils.quote(query)}"
    data = fetch_json(url, BSE_HEADERS)

    # Response is a list of dicts with keys: SECURITY_CODE, SECURITY_NAME, ...
    if isinstance(data, list) and data:
        first = data[0]
        code  = str(first.get("SECURITY_CODE", "")).strip()
        name  = first.get("SECURITY_NAME") or first.get("Issuer_Name") or query
        if code:
            log(f"[BSE] Found: {name} ({code})", logs)
            return code, name

    # Fallback — try alternate endpoint
    url2  = f"https://api.bseindia.com/BseIndiaAPI/api/GetCompanyList/w?search={requests.utils.quote(query)}"
    data2 = fetch_json(url2, BSE_HEADERS)
    if isinstance(data2, list) and data2:
        first = data2[0]
        code  = str(first.get("scripcode") or first.get("SCRIP_CD") or "").strip()
        name  = first.get("long_name") or first.get("SCRIP_NAME") or query
        if code:
            log(f"[BSE] Found (alt): {name} ({code})", logs)
            return code, name

    log(f"[BSE] ❌ Company not found: '{query}'", logs)
    return None, None


def bse_get_report_url(scrip_code: str, year: int, logs: list) -> str | None:
    """Fetch annual report list and return PDF URL matching target year."""
    log(f"[BSE] Fetching report list for {scrip_code}...", logs)
    url  = f"https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode={scrip_code}"
    data = fetch_json(url, BSE_HEADERS)

    if not isinstance(data, list) or not data:
        # Try alternate report endpoint
        url2 = f"https://api.bseindia.com/BseIndiaAPI/api/AnnRptList/w?scripcode={scrip_code}&type=C"
        data = fetch_json(url2, BSE_HEADERS)

    if not isinstance(data, list):
        log(f"[BSE] ❌ No report list returned", logs)
        return None

    target_str   = str(year)
    target_short = target_str[-2:]          # "24" for 2024

    for item in data:
        # Fields vary: PERIOD, YEAR, PERIOD_END, FILENAME, PDFNAME, ...
        period = str(
            item.get("PERIOD") or item.get("YEAR") or
            item.get("PERIOD_END") or item.get("year") or ""
        )
        pdf_field = (
            item.get("FILENAME") or item.get("PDFNAME") or
            item.get("DOCUMENT_NAME") or item.get("PDF_LINK") or ""
        )

        period_l = period.lower()
        if target_str in period_l or f"-{target_short}" in period_l or f"/{target_short}" in period_l:
            # Build full URL if relative
            if pdf_field:
                if pdf_field.startswith("http"):
                    return pdf_field
                # Common BSE PDF paths
                for base in [
                    "https://www.bseindia.com/xml-data/corpfiling/AttachHis/",
                    "https://www.bseindia.com/AnnualReports/",
                    "https://www.bseindia.com/",
                ]:
                    candidate = base + pdf_field
                    log(f"[BSE] Candidate URL: {candidate}", logs)
                    return candidate

    log(f"[BSE] ❌ No report found for year {year} (checked {len(data)} entries)", logs)
    return None


def handle_bse(company: str, year: int, logs: list) -> dict | None:
    code, name = bse_search_company(company, logs)
    if not code:
        return None

    pdf_url = bse_get_report_url(code, year, logs)
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
        data = data.get("data") or data.get("reports") or []

    if not isinstance(data, list) or not data:
        log(f"[NSE] ❌ No report list returned", logs)
        return None

    # NSE year format: "2023-24" for FY starting 2023
    target_short = str(year + 1)[-2:]
    pattern      = f"{year}-{target_short}"     # e.g. "2023-24"

    for item in data:
        period = str(
            item.get("toDate") or item.get("fromDate") or
            item.get("year")   or item.get("yearRange") or ""
        )
        pdf_url = item.get("fileName") or item.get("pdfLink") or item.get("url") or ""

        if pattern in period or str(year) in period:
            if pdf_url:
                full = pdf_url if pdf_url.startswith("http") else f"https://www.nseindia.com{pdf_url}"
                log(f"[NSE] Found report URL for {pattern}", logs)
                return full

    log(f"[NSE] ❌ No report found for {pattern} (checked {len(data)} entries)", logs)
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
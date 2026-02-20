import os
import io
import re
import sys
import time
import asyncio
import zipfile
import platform
import subprocess
import requests
import streamlit as st
from datetime import datetime
from playwright_stealth import Stealth
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────
# CRITICAL — must run before ANY Playwright import or call.
#
# Python 3.13 on Windows changed the default event loop to
# SelectorEventLoop, which does NOT support subprocess spawning.
# Playwright (even its sync API) internally spawns the browser
# process via asyncio.create_subprocess_exec, so it needs
# ProactorEventLoop on Windows — always.
#
# This must be set at module level, not inside a function.
# ─────────────────────────────────────────────
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    _loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(_loop)

# ─────────────────────────────────────────────
# Auto-install Playwright browser once per container (Streamlit Cloud)
# ─────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def install_playwright_browser():
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False, capture_output=True
    )
    if platform.system() != "Windows":
        subprocess.run(
            [sys.executable, "-m", "playwright", "install-deps", "chromium"],
            check=False, capture_output=True
        )

install_playwright_browser()

# ─────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Annual Report Downloader",
    page_icon="📄",
    layout="wide",
)

st.markdown("""
<style>
    .stButton > button { font-weight: 600; border-radius: 8px; padding: 0.45rem 1.2rem; }
    .log-box {
        background: #1e1e2e; color: #cdd6f4;
        font-family: 'Courier New', monospace; font-size: 0.82rem;
        border-radius: 8px; padding: 1rem 1.2rem;
        max-height: 420px; overflow-y: auto;
        white-space: pre-wrap; line-height: 1.6;
    }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
CONFIG = {
    "timeout_navigation": 45_000,
    "timeout_element":    15_000,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "delay_between_companies": 2,
}

# ─────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────
def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def log(msg: str, logs: list):
    ts = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{ts}] {msg}")


def download_bytes(url: str, exchange: str = "") -> bytes | None:
    headers = {"User-Agent": CONFIG["user_agent"], "Referer": "https://www.google.com/"}
    if "nseindia" in url:
        headers["Referer"] = "https://www.nseindia.com/"
    elif "bseindia" in url:
        headers["Referer"] = "https://www.bseindia.com/"
    try:
        r = requests.get(url, headers=headers, allow_redirects=True, timeout=60)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


# ─────────────────────────────────────────────
# BSE — fully synchronous
# ─────────────────────────────────────────────
def bse_discover(company_input: str, logs: list):
    company_input = company_input.strip()

    if "bseindia.com/stock-share-price/" in company_input:
        m = re.search(r'/stock-share-price/([^/]+)/([^/]+)/(\d+)/', company_input)
        if m:
            return m.group(3), m.group(2), m.group(1), m.group(2)

    if company_input.isdigit() and len(company_input) == 6:
        return company_input, "symbol", "company", company_input

    log(f"[BSE] Searching for '{company_input}'...", logs)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=CONFIG["user_agent"])
        page    = context.new_page()
        try:
            page.goto("https://www.bseindia.com/getquote.aspx",
                      wait_until="networkidle", timeout=CONFIG["timeout_navigation"])

            search_sel     = "input#ContentPlaceHolder1_SmartSearch_smartSearch"
            suggestion_sel = "#ajax_response_smart li a"

            page.wait_for_selector(search_sel, timeout=CONFIG["timeout_element"])
            page.focus(search_sel)

            for char in company_input[:30]:
                page.keyboard.press(char)
                time.sleep(0.1)

            try:
                page.wait_for_selector(suggestion_sel, timeout=10_000)
                for link in page.locator(suggestion_sel).all():
                    text  = link.inner_text()
                    codes = re.findall(r'(\d{6})', text)
                    if codes:
                        name = text.splitlines()[0] if text.splitlines() else company_input
                        browser.close()
                        return codes[-1], "symbol", "company", name
            except Exception:
                pass

            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=CONFIG["timeout_navigation"])
            m = re.search(r'/stock-share-price/([^/]+)/([^/]+)/(\d+)/', page.url)
            if m:
                browser.close()
                return m.group(3), m.group(2), m.group(1), company_input

        except Exception as e:
            log(f"[BSE-ERROR] Discovery failed: {e}", logs)
        browser.close()

    return None, None, None, None


def bse_extract_reports(page) -> list:
    reports = []
    try:
        try:
            page.wait_for_selector("td:has-text('Year')", timeout=10_000)
        except Exception:
            page.wait_for_selector("table", timeout=5_000)

        try:
            table = page.locator("table").filter(
                has=page.locator("td:has-text('Year')")
            ).last
        except Exception:
            table = page.locator("table").last

        for row in table.locator("tr").all():
            cells = row.locator("td").all()
            if not cells:
                continue
            period = cells[0].inner_text().strip()
            if not period or not any(ch.isdigit() for ch in period):
                continue
            for link in row.locator("a").all():
                href = link.get_attribute("href") or ""
                hl   = href.lower()
                if any(x in hl for x in [".pdf", "/AttachHis/", "/AnnualReport/", "/HISTANNR/"]):
                    reports.append({
                        "year_str": period,
                        "link": href if href.startswith("http") else f"https://www.bseindia.com{href}",
                    })
                    break
    except Exception:
        pass
    return reports


def handle_bse(company: str, year: int, logs: list) -> dict | None:
    code, symbol, slug, name = bse_discover(company, logs)
    if not code:
        log(f"[BSE] Could not find '{company}'", logs)
        return None

    log(f"[BSE] Found: {name} ({code})", logs)
    url = f"https://www.bseindia.com/stock-share-price/{slug}/{symbol}/{code}/financials-annual-reports/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=CONFIG["timeout_navigation"])
            time.sleep(2)
            reports = bse_extract_reports(page)

            target_str   = str(year)
            target_short = target_str[-2:]

            for r in reports:
                yt = r["year_str"].lower()
                if target_str in yt or f"-{target_short}" in yt or f"/{target_short}" in yt:
                    log(f"[BSE] Downloading: {r['year_str']}...", logs)
                    data = download_bytes(r["link"], "bse")
                    if data:
                        fname = sanitize_filename(f"BSE_{name}_{year}_AnnualReport.pdf")
                        log(f"[BSE] ✅ {fname} ({len(data)/1048576:.2f} MB)", logs)
                        browser.close()
                        return {"filename": fname, "data": data, "exchange": "BSE", "company": name}
                    else:
                        log(f"[BSE] ❌ Download failed: {r['link']}", logs)
                    break

            log(f"[BSE] No report found for year {year}", logs)
        except Exception as e:
            log(f"[BSE-ERROR] {e}", logs)
        browser.close()

    return None


# ─────────────────────────────────────────────
# NSE — fully synchronous
# ─────────────────────────────────────────────
def nse_discover(company_input: str, logs: list):
    log(f"[NSE] Searching for '{company_input}'...", logs)
    headers = {"User-Agent": CONFIG["user_agent"], "Accept": "application/json"}
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=15)
        r    = session.get(
            f"https://www.nseindia.com/api/search/autocomplete?q={company_input}",
            headers=headers, timeout=15
        )
        data    = r.json()
        results = data if isinstance(data, list) else (data.get("symbols") or data.get("data") or [])
        if results:
            first = results[0]
            return first.get("symbol"), first.get("companyName") or first.get("symbol")
    except Exception as e:
        log(f"[NSE-ERROR] API search failed: {e}", logs)
    return None, None


def nse_extract_reports(page, year: int) -> list:
    short   = str(year + 1)[-2:]
    pattern = f"{year}-{short}"
    reports = []
    try:
        page.wait_for_selector("a[href$='.pdf'], a[href$='.zip']", timeout=15_000)
        for link in page.locator("a[href$='.pdf'], a[href$='.zip']").all():
            href = link.get_attribute("href") or ""
            text = link.text_content() or ""
            if str(year) in text or pattern in text or str(year) in href or pattern in href:
                reports.append({
                    "year_str": pattern,
                    "link": href if href.startswith("http") else f"https://www.nseindia.com{href}",
                })
                break
    except Exception:
        pass
    return reports


def handle_nse(company: str, year: int, logs: list) -> dict | None:
    symbol, name = nse_discover(company, logs)
    if not symbol:
        log(f"[NSE] Could not find '{company}'", logs)
        return None

    log(f"[NSE] Found: {name} ({symbol})", logs)
    url = f"https://www.nseindia.com/companies-listing/corporate-filings-annual-reports?symbol={symbol}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=CONFIG["user_agent"])
        page    = context.new_page()
        try:
            page.goto("https://www.nseindia.com", wait_until="networkidle",
                      timeout=CONFIG["timeout_navigation"])
            page.goto(url, wait_until="networkidle", timeout=CONFIG["timeout_navigation"])
            time.sleep(3)

            reports = nse_extract_reports(page, year)
            if reports:
                r    = reports[0]
                log(f"[NSE] Downloading: {r['year_str']}...", logs)
                data = download_bytes(r["link"], "nse")
                if data:
                    fname = sanitize_filename(f"NSE_{name}_{year}_AnnualReport.pdf")
                    log(f"[NSE] ✅ {fname} ({len(data)/1048576:.2f} MB)", logs)
                    browser.close()
                    return {"filename": fname, "data": data, "exchange": "NSE", "company": name}
                else:
                    log(f"[NSE] ❌ Download failed: {r['link']}", logs)
            else:
                log(f"[NSE] No report found for year {year}", logs)
        except Exception as e:
            log(f"[NSE-ERROR] {e}", logs)
        browser.close()

    return None


# ─────────────────────────────────────────────
# Core runner — plain for-loop, zero asyncio
# ─────────────────────────────────────────────
def run_downloads(companies: list, year: int, exchange: str, logs: list) -> list:
    results = []
    for company in companies:
        log(f"\n{'─'*50}", logs)
        log(f"Processing: {company}", logs)
        if exchange in ("BSE", "BOTH"):
            res = handle_bse(company, year, logs)
            if res:
                results.append(res)
        if exchange in ("NSE", "BOTH"):
            res = handle_nse(company, year, logs)
            if res:
                results.append(res)
        time.sleep(CONFIG["delay_between_companies"])

    log(f"\n{'─'*50}", logs)
    log(f"Job complete — {len(results)} file(s) ready.", logs)
    return results


# ─────────────────────────────────────────────
# Session state init
# ─────────────────────────────────────────────
for _key, _default in [("results", []), ("logs", []), ("ran", False)]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────
st.title("📄 Annual Report Downloader")
st.caption("Download BSE & NSE annual reports — no login required.")
st.divider()

col_left, col_right = st.columns([3, 1], gap="large")

with col_left:
    batch_mode = st.toggle("Batch mode (multiple companies)", value=False)
    if batch_mode:
        raw = st.text_area(
            "Company names — one per line or comma-separated",
            value="Reliance Industries\nHCL Technologies",
            height=130,
        )
        companies = [c.strip() for line in raw.splitlines() for c in line.split(",") if c.strip()]
        st.caption(f"🏢 {len(companies)} company/companies detected")
    else:
        single = st.text_input(
            "Company name, BSE URL, or 6-digit scrip code",
            value="Reliance Industries",
        )
        companies = [single.strip()] if single.strip() else []

with col_right:
    year = st.number_input(
        "Target Year", min_value=2000, max_value=datetime.now().year,
        value=2024, step=1,
    )
    st.caption("BSE → FY ending this year  \nNSE → FY starting this year")
    exchange = st.radio("Exchange", ["BSE", "NSE", "BOTH"], horizontal=True)

st.divider()

if st.button("🚀 Fetch Reports", type="primary",
             disabled=(len(companies) == 0), use_container_width=True):
    st.session_state.results = []
    st.session_state.logs    = []
    st.session_state.ran     = True

    logs: list = []
    with st.spinner("Fetching... this may take a few minutes."):
        # ✅ Plain synchronous call — no asyncio, no event loops, no threading
        results = run_downloads(companies, year, exchange, logs)

    st.session_state.results = results
    st.session_state.logs    = logs

# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────
if st.session_state.ran:
    st.divider()
    results = st.session_state.results
    logs    = st.session_state.logs

    c1, c2, c3 = st.columns(3)
    c1.metric("Companies queued", len(companies))
    c2.metric("Files downloaded", len(results))
    c3.metric("Exchanges hit",    len({r["exchange"] for r in results}))

    if results:
        st.subheader("📥 Download Files")
        for r in results:
            color = "#3498db" if r["exchange"] == "BSE" else "#e67e22"
            col_a, col_b = st.columns([4, 1])
            with col_a:
                st.markdown(
                    f'<span style="background:{color};color:white;padding:2px 8px;'
                    f'border-radius:4px;font-size:0.78rem;">{r["exchange"]}</span>&nbsp; '
                    f'**{r["company"]}** — `{r["filename"]}`'
                    f' <span style="color:#7f8c8d;font-size:0.8rem;">'
                    f'({len(r["data"])/1048576:.2f} MB)</span>',
                    unsafe_allow_html=True,
                )
            with col_b:
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
                file_name=f"AnnualReports_{year}.zip", mime="application/zip",
                use_container_width=True,
            )
    else:
        st.warning("No files downloaded. Check the activity log below for details.")

    st.subheader("🖥 Activity Log")
    st.markdown(
        f'<div class="log-box">{"<br>".join(logs)}</div>',
        unsafe_allow_html=True,
    )

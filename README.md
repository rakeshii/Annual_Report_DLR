# 📄 Annual Report Downloader — NSE & BSE

A **Streamlit** web application that lets you download annual reports (PDFs) from **BSE India** and **NSE India** — no login, no API key required.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔍 Smart search | Search by company name, 6-digit BSE scrip code, or full BSE URL |
| 🏦 Dual exchange | Fetch from BSE, NSE, or **both** simultaneously |
| 📦 Batch mode | Queue multiple companies at once |
| ⬇ Direct download | PDF files streamed straight to your browser |
| 🗜 ZIP bundle | Download all results as a single ZIP when fetching multiple files |

---

## 🚀 Run Locally

### 1. Clone the repo
```bash
git clone https://github.com/<your-username>/Annual_Report_DLR.git
cd Annual_Report_DLR
```

### 2. Create and activate a virtual environment
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 3. Install Python dependencies
```bash
pip install -r requirements.txt
```

### 4. Install Playwright's Chromium browser
```bash
playwright install chromium
```

### 5. Launch the app
```bash
streamlit run NSE_BSE_IRP.py
```

The app opens at **http://localhost:8501** automatically.

---

## ☁ Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Select your repo, branch (`main`), and set **Main file path** to `NSE_BSE_IRP.py`.
4. Click **Deploy** — Playwright and its Chromium browser are installed automatically on first boot via `packages.txt` and the `install_playwright_browser()` cache call in the app.

> **Note:** First cold start takes ~2–3 minutes while Chromium installs.

---

## 🗂 Repository Structure

```
Annual_Report_DLR/
├── NSE_BSE_IRP.py          # Main Streamlit application
├── requirements.txt         # Python dependencies
├── packages.txt             # System-level apt packages (Streamlit Cloud)
├── .streamlit/
│   └── config.toml          # Streamlit server & theme settings
├── .gitignore
└── README.md
```

---

## ⚙ Configuration

Key timeouts and delays live in the `CONFIG` dict at the top of `NSE_BSE_IRP.py`:

```python
CONFIG = {
    "timeout_navigation": 45_000,   # ms — page load timeout
    "timeout_element":    15_000,   # ms — element wait timeout
    "delay_between_companies": 2,   # seconds between batch requests
}
```

---

## 🛠 Tech Stack

- [Streamlit](https://streamlit.io) — UI framework
- [Playwright](https://playwright.dev/python/) — headless browser automation (BSE search & NSE scraping)
- [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) — bot-detection bypass
- [Requests](https://docs.python-requests.org/) — direct PDF downloads & NSE autocomplete API

---

## 📝 License

MIT — free to use and modify.
# Annual_Report_DLR

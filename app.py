"""
Amazon Watchdog — Web App (Flask)
Upload CSVs → get premium HTML audit report.
"""

import os
import uuid
import json
import shutil
from pathlib import Path
from datetime import datetime

from flask import Flask, request, redirect, url_for, render_template_string, send_file, abort

# Import our pipeline modules
from run_audit import (
    load_and_detect, summarize_business_report, summarize_search_terms,
    summarize_inventory, summarize_returns, compute_cross_report_flags,
    build_claude_input, call_claude, build_pdf
)
from report_template import generate_html

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max total upload

UPLOAD_DIR = Path(__file__).parent / "uploads"
OUTPUT_DIR = Path(__file__).parent / "output"

# ─── FREE TIER LIMITS ──────────────────────────────────────────────────────────
MAX_ROWS_FREE = {
    "business_report": 500,
    "search_term_report": 5000,
    "inventory_health": 500,
    "customer_returns": 2000,
}
MAX_AUDITS_PER_DAY = 20  # total across all users for cost control
daily_audit_count = {"date": "", "count": 0}


def check_daily_limit():
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_audit_count["date"] != today:
        daily_audit_count["date"] = today
        daily_audit_count["count"] = 0
    if daily_audit_count["count"] >= MAX_AUDITS_PER_DAY:
        return False
    return True


def increment_daily_count():
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_audit_count["date"] != today:
        daily_audit_count["date"] = today
        daily_audit_count["count"] = 0
    daily_audit_count["count"] += 1


# ─── TEMPLATES ─────────────────────────────────────────────────────────────────

UPLOAD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Amazon Watchdog — Performance Audit</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', sans-serif;
    background: #fafafa;
    color: #1a1a2e;
    -webkit-font-smoothing: antialiased;
}
.container { max-width: 640px; margin: 0 auto; padding: 0 24px; }
.hero {
    text-align: center;
    padding: 60px 0 40px;
}
.brand {
    font-size: 11px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: #9ca3af;
    margin-bottom: 16px;
}
h1 {
    font-size: 36px;
    font-weight: 700;
    letter-spacing: -1px;
    line-height: 1.2;
    margin-bottom: 12px;
}
.subtitle {
    font-size: 16px;
    color: #6b7280;
    margin-bottom: 40px;
}
.upload-card {
    background: white;
    border: 2px dashed #d1d5db;
    border-radius: 16px;
    padding: 40px;
    text-align: center;
    margin-bottom: 24px;
    transition: all 0.2s;
}
.upload-card:hover { border-color: #2563eb; background: #f8faff; }
.upload-card h3 { font-size: 18px; margin-bottom: 8px; }
.upload-card p { font-size: 13px; color: #6b7280; margin-bottom: 16px; }
input[type="file"] {
    display: block;
    margin: 0 auto;
    font-size: 14px;
}
.submit-btn {
    display: block;
    width: 100%;
    padding: 16px;
    background: #1a1a2e;
    color: white;
    border: none;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    margin-top: 24px;
    transition: all 0.2s;
}
.submit-btn:hover { background: #374151; transform: translateY(-1px); }
.submit-btn:disabled { background: #9ca3af; cursor: not-allowed; transform: none; }
.note {
    text-align: center;
    font-size: 12px;
    color: #9ca3af;
    margin-top: 16px;
    line-height: 1.6;
}
.error {
    background: #fef2f2;
    border: 1px solid #fecaca;
    color: #dc2626;
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 24px;
    font-size: 14px;
}
.processing {
    text-align: center;
    padding: 80px 0;
}
.spinner {
    width: 48px;
    height: 48px;
    border: 4px solid #e5e7eb;
    border-top-color: #1a1a2e;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 24px;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="container">
    <div class="hero">
        <div class="brand">Amazon Watchdog</div>
        <h1>Amazon Performance<br>Audit</h1>
        <p class="subtitle">Upload your Seller Central reports. Get an independent analysis in under 60 seconds.</p>
    </div>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    <form method="POST" action="/upload" enctype="multipart/form-data" id="uploadForm">
        <div class="upload-card">
            <h3>Upload your reports</h3>
            <p>Select 1-4 CSV or XLSX files from Seller Central.<br>
            Business Report, Search Term Report, Inventory Health, Customer Returns.</p>
            <input type="file" name="files" multiple accept=".csv,.xlsx,.xls,.txt" required>
        </div>

        <button type="submit" class="submit-btn" id="submitBtn">Run Audit</button>
    </form>

    <p class="note">
        Your data is processed and deleted immediately. We never store your files.<br>
        Reports are auto-detected by their column structure — filenames don't matter.
    </p>
</div>

<script>
document.getElementById('uploadForm').addEventListener('submit', function() {
    document.getElementById('submitBtn').disabled = true;
    document.getElementById('submitBtn').textContent = 'Analyzing... (up to 60 seconds)';
});
</script>
</body>
</html>"""

ERROR_PAGE = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>
body { font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; background: #fafafa; }
.box { text-align: center; max-width: 400px; }
h1 { font-size: 24px; margin-bottom: 12px; }
p { color: #6b7280; margin-bottom: 24px; }
a { display: inline-block; padding: 12px 24px; background: #1a1a2e; color: white; text-decoration: none; border-radius: 8px; }
</style></head>
<body><div class="box">
<h1>{{ title }}</h1>
<p>{{ message }}</p>
<a href="/">Try Again</a>
</div></body></html>"""


# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(UPLOAD_PAGE, error=None)


@app.route("/upload", methods=["POST"])
def upload():
    # Check daily limit
    if not check_daily_limit():
        return render_template_string(ERROR_PAGE,
            title="Daily limit reached",
            message="We've hit the daily audit limit for this test environment. Try again tomorrow."
        ), 429

    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return render_template_string(UPLOAD_PAGE, error="Please select at least one file.")

    # Create temp directory for this audit
    audit_id = str(uuid.uuid4())[:8]
    audit_dir = UPLOAD_DIR / audit_id
    audit_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Save uploaded files
        saved = []
        for f in files:
            if f.filename:
                safe_name = f"{uuid.uuid4().hex[:8]}_{Path(f.filename).suffix}"
                filepath = audit_dir / safe_name
                f.save(str(filepath))
                saved.append(str(filepath))

        if not saved:
            return render_template_string(UPLOAD_PAGE, error="No valid files uploaded.")

        # Parse and detect
        reports = {}
        for filepath in saved:
            rtype, headers, rows = load_and_detect(filepath)
            if rtype:
                # Apply row limits for cost control
                max_rows = MAX_ROWS_FREE.get(rtype, 1000)
                if len(rows) > max_rows:
                    rows = rows[:max_rows]
                reports[rtype] = {"headers": headers, "rows": rows}

        if not reports:
            return render_template_string(ERROR_PAGE,
                title="Unrecognized files",
                message="None of the uploaded files could be identified as Amazon Seller Central reports. Please check that you're uploading the correct CSV/XLSX files."
            )

        # Summarize
        summaries = {}
        biz_summary = None
        if "business_report" in reports:
            biz_summary = summarize_business_report(reports["business_report"]["rows"])
            summaries["business_report"] = biz_summary

        ppc_summary = None
        if "search_term_report" in reports:
            ppc_summary = summarize_search_terms(reports["search_term_report"]["rows"])
            summaries["search_term_report"] = ppc_summary

        inv_summary = None
        if "inventory_health" in reports:
            inv_summary = summarize_inventory(reports["inventory_health"]["rows"])
            summaries["inventory_health"] = inv_summary

        ret_summary = None
        if "customer_returns" in reports:
            biz_asin_map = biz_summary.get("asinMap") if biz_summary else None
            ret_summary = summarize_returns(reports["customer_returns"]["rows"], biz_asin_map)
            summaries["customer_returns"] = ret_summary

        # Cross-report flags
        flags = compute_cross_report_flags(biz_summary, ppc_summary, inv_summary, ret_summary)

        # Build Claude input
        claude_input = build_claude_input(
            biz_summary, ppc_summary, inv_summary, ret_summary,
            flags, list(reports.keys())
        )

        # Call Claude
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return render_template_string(ERROR_PAGE,
                title="Configuration error",
                message="The AI analysis service is not configured. Please contact the admin."
            )

        audit_result = call_claude(claude_input)
        increment_daily_count()

        # Generate HTML report
        html_full = generate_html(audit_result, summaries, is_paid=True)
        html_free = generate_html(audit_result, summaries, is_paid=False)

        # Save reports
        OUTPUT_DIR.mkdir(exist_ok=True)
        full_path = OUTPUT_DIR / f"{audit_id}_full.html"
        free_path = OUTPUT_DIR / f"{audit_id}_free.html"
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(html_full)
        with open(free_path, "w", encoding="utf-8") as f:
            f.write(html_free)

        # Save JSON for debugging
        json_path = OUTPUT_DIR / f"{audit_id}_output.json"
        with open(json_path, "w") as f:
            json.dump(audit_result, f, indent=2, default=str)

        # Redirect to free report (full report behind paywall in production)
        # For testing: show full report
        return redirect(url_for("view_report", audit_id=audit_id, version="full"))

    finally:
        # Always delete uploaded files immediately
        shutil.rmtree(audit_dir, ignore_errors=True)


@app.route("/report/<audit_id>/<version>")
def view_report(audit_id, version):
    if version not in ("full", "free"):
        abort(404)
    filepath = OUTPUT_DIR / f"{audit_id}_{version}.html"
    if not filepath.exists():
        return render_template_string(ERROR_PAGE,
            title="Report not found",
            message="This report may have expired or the link is invalid."
        ), 404
    return send_file(str(filepath), mimetype="text/html")


# ─── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    UPLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)

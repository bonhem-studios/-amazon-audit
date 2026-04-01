"""
Amazon Performance Audit — Web App (Flask)
Upload CSVs → get premium HTML audit report.
"""

import os
import uuid
import json
import shutil
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, request, redirect, url_for, render_template_string, send_file, abort, jsonify

# Import our pipeline modules
from run_audit import (
    load_and_detect, summarize_business_report, summarize_search_terms,
    summarize_inventory, summarize_returns, compute_cross_report_flags,
    build_claude_input, call_claude, build_pdf
)
from report_template import generate_html

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max total upload

import logging
import traceback
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Job tracking for async processing
jobs = {}  # audit_id -> {"status": "processing"|"done"|"error", "error": str, "step": str}


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
<title>Amazon Performance Audit — Performance Audit</title>
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
.submit-btn:disabled { background: #d1d5db; color: #9ca3af; cursor: not-allowed; transform: none; }
.file-count-warning {
    background: #fffbeb;
    border: 1px solid #fde68a;
    color: #92400e;
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.5;
    margin-top: 16px;
    text-align: center;
}
.note {
    text-align: center;
    font-size: 12px;
    color: #9ca3af;
    margin-top: 16px;
    line-height: 1.6;
}
/* Report instruction cards */
.report-cards {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 20px;
}
.report-card {
    background: white;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 14px 16px;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
}
.report-card:hover { border-color: #2563eb; background: #f8faff; }
.report-icon {
    width: 28px; height: 28px;
    background: #1a1a2e;
    color: white;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 700;
    flex-shrink: 0;
}
.report-info { flex: 1; min-width: 0; }
.report-info strong { display: block; font-size: 13px; line-height: 1.3; }
.report-info span { font-size: 11px; color: #9ca3af; }
.report-help {
    font-size: 11px;
    color: #2563eb;
    width: 100%;
    text-align: right;
    margin-top: -4px;
}

/* Upload drop zone */
.upload-card {
    background: white;
    border: 2px dashed #d1d5db;
    border-radius: 16px;
    padding: 32px;
    text-align: center;
    transition: all 0.2s;
    cursor: pointer;
    position: relative;
}
.upload-card:hover { border-color: #2563eb; background: #f8faff; }
.upload-icon { margin-bottom: 12px; }
.upload-card p { font-size: 14px; color: #6b7280; margin-bottom: 4px; }
.upload-hint { font-size: 12px; color: #9ca3af; }
.upload-card input[type="file"] {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
}
.file-list { margin-top: 12px; text-align: left; }
.file-item {
    display: flex;
    justify-content: space-between;
    padding: 6px 10px;
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    border-radius: 6px;
    margin-top: 6px;
    font-size: 13px;
}
.file-size { color: #9ca3af; }

/* Modal */
.modal {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s;
}
.modal.active { opacity: 1; pointer-events: all; }
.modal-backdrop {
    position: absolute;
    inset: 0;
    background: rgba(0,0,0,0.4);
    backdrop-filter: blur(4px);
}
.modal-content {
    position: relative;
    background: white;
    border-radius: 16px;
    padding: 32px;
    max-width: 480px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 24px 64px rgba(0,0,0,0.15);
    transform: translateY(10px);
    transition: transform 0.2s;
}
.modal.active .modal-content { transform: translateY(0); }
.modal-close {
    position: absolute;
    top: 16px;
    right: 20px;
    background: none;
    border: none;
    font-size: 24px;
    color: #9ca3af;
    cursor: pointer;
    line-height: 1;
}
.modal-close:hover { color: #1a1a2e; }
.modal-content h3 { font-size: 20px; margin-bottom: 4px; }
.modal-subtitle { font-size: 13px; color: #9ca3af; margin-bottom: 20px; }
.steps {
    list-style: none;
    padding: 0;
    counter-reset: step;
}
.steps li {
    counter-increment: step;
    padding: 10px 0 10px 36px;
    position: relative;
    font-size: 14px;
    line-height: 1.5;
    border-bottom: 1px solid #f3f4f6;
}
.steps li:last-child { border-bottom: none; }
.steps li::before {
    content: counter(step);
    position: absolute;
    left: 0;
    top: 10px;
    width: 24px;
    height: 24px;
    background: #eff6ff;
    color: #2563eb;
    border-radius: 50%;
    font-size: 12px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
}
.modal-note {
    margin-top: 16px;
    padding: 12px 14px;
    background: #f8fafc;
    border-radius: 8px;
    font-size: 12px;
    color: #6b7280;
    line-height: 1.5;
}

/* Progress overlay */
.progress-overlay {
    position: fixed;
    inset: 0;
    z-index: 2000;
    background: rgba(250,250,250,0.97);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.3s;
}
.progress-overlay.active { opacity: 1; pointer-events: all; }
.progress-box {
    text-align: center;
    max-width: 440px;
    width: 90%;
}
.progress-spinner {
    width: 48px;
    height: 48px;
    border: 3px solid #e5e7eb;
    border-top-color: #1a1a2e;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin: 0 auto 24px;
}
@keyframes spin { to { transform: rotate(360deg); } }
.progress-box h2 {
    font-size: 22px;
    font-weight: 700;
    margin-bottom: 4px;
}
.progress-sub {
    font-size: 14px;
    color: #9ca3af;
    margin-bottom: 32px;
}
.progress-steps {
    text-align: left;
    margin: 0 auto;
    max-width: 360px;
}
.progress-step {
    display: flex;
    gap: 14px;
    padding: 12px 0;
    opacity: 0.3;
    transition: opacity 0.4s;
}
.progress-step.done { opacity: 1; }
.progress-step.active { opacity: 1; }
.step-dot {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 2px solid #d1d5db;
    flex-shrink: 0;
    margin-top: 2px;
    position: relative;
    transition: all 0.3s;
}
.progress-step.active .step-dot {
    border-color: #2563eb;
    background: #2563eb;
    animation: pulse-dot 1.5s ease-in-out infinite;
}
.progress-step.active .step-dot::after {
    content: '';
    position: absolute;
    inset: 4px;
    background: white;
    border-radius: 50%;
}
.progress-step.done:not(.active) .step-dot {
    border-color: #059669;
    background: #059669;
}
.progress-step.done:not(.active) .step-dot::after {
    content: '\\2713';
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: 12px;
    font-weight: bold;
}
@keyframes pulse-dot {
    0%, 100% { box-shadow: 0 0 0 0 rgba(37,99,235,0.3); }
    50% { box-shadow: 0 0 0 8px rgba(37,99,235,0); }
}
.step-text strong {
    display: block;
    font-size: 13px;
    color: #1a1a2e;
    line-height: 1.3;
}
.step-text span {
    font-size: 12px;
    color: #9ca3af;
}
.progress-warn {
    margin-top: 24px;
    font-size: 13px;
    color: #d97706;
    background: #fffbeb;
    padding: 10px 14px;
    border-radius: 8px;
    border: 1px solid #fde68a;
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
        <div class="brand">Amazon Performance Audit</div>
        <h1>Amazon Performance<br>Audit</h1>
        <p class="subtitle">Upload your Seller Central reports. Get an independent analysis in under 60 seconds.</p>
    </div>

    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    <form method="POST" action="/upload" enctype="multipart/form-data" id="uploadForm">

        <div class="report-cards">
            <div class="report-card" onclick="document.getElementById('helpModal1').classList.add('active')">
                <div class="report-icon">1</div>
                <div class="report-info">
                    <strong>Business Report</strong>
                    <span>Sales & Traffic by ASIN</span>
                </div>
                <div class="report-help">How to download &#8250;</div>
            </div>

            <div class="report-card" onclick="document.getElementById('helpModal2').classList.add('active')">
                <div class="report-icon">2</div>
                <div class="report-info">
                    <strong>Search Term Report</strong>
                    <span>Sponsored Products PPC</span>
                </div>
                <div class="report-help">How to download &#8250;</div>
            </div>

            <div class="report-card" onclick="document.getElementById('helpModal3').classList.add('active')">
                <div class="report-icon">3</div>
                <div class="report-info">
                    <strong>Inventory Health</strong>
                    <span>FBA Stock Levels</span>
                </div>
                <div class="report-help">How to download &#8250;</div>
            </div>

            <div class="report-card" onclick="document.getElementById('helpModal4').classList.add('active')">
                <div class="report-icon">4</div>
                <div class="report-info">
                    <strong>Customer Returns</strong>
                    <span>FBA Return Reasons</span>
                </div>
                <div class="report-help">How to download &#8250;</div>
            </div>
        </div>

        <div class="upload-card">
            <div class="upload-icon">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" stroke-width="1.5">
                    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
                </svg>
            </div>
            <p>Drop your files here or click to browse</p>
            <span class="upload-hint">CSV or XLSX &middot; all 4 reports required &middot; auto-detected</span>
            <input type="file" name="files" multiple accept=".csv,.xlsx,.xls,.txt" required id="fileInput">
            <div id="fileList" class="file-list"></div>
        </div>

        <div class="file-count-warning" id="fileWarning" style="display:none;">
            <strong>Please upload all 4 reports</strong> for a complete analysis.
            Click the report cards above to see where to download each one.
        </div>

        <button type="submit" class="submit-btn" id="submitBtn" disabled>Upload all 4 reports to start</button>
    </form>

    <p class="note">
        Your data is processed and deleted immediately. We never store your files.<br>
        Reports are auto-detected by their column structure — filenames don't matter.
    </p>
</div>

<!-- PROGRESS OVERLAY -->
<div class="progress-overlay" id="progressOverlay">
    <div class="progress-box">
        <div class="progress-spinner"></div>
        <h2>Analyzing your data</h2>
        <p class="progress-sub">This may take a few minutes depending on data size</p>

        <div class="progress-steps">
            <div class="progress-step active" id="step1">
                <div class="step-dot"></div>
                <div class="step-text">
                    <strong>Uploading & validating files</strong>
                    <span>Checking column structure, detecting report types</span>
                </div>
            </div>
            <div class="progress-step" id="step2">
                <div class="step-dot"></div>
                <div class="step-text">
                    <strong>Cross-referencing reports</strong>
                    <span>Aggregating at ASIN level, computing inventory & return metrics</span>
                </div>
            </div>
            <div class="progress-step" id="step3">
                <div class="step-dot"></div>
                <div class="step-text">
                    <strong>Analysis in progress</strong>
                    <span>Identifying findings, estimating impact, generating recommendations</span>
                </div>
            </div>
            <div class="progress-step" id="step4">
                <div class="step-dot"></div>
                <div class="step-text">
                    <strong>Building your report</strong>
                    <span>Almost done...</span>
                </div>
            </div>
        </div>

        <p class="progress-warn" id="progressWarn" style="display:none;">Taking a bit longer than usual — large datasets need more time. Please wait...</p>
    </div>
</div>

<!-- INSTRUCTION MODALS -->
<div class="modal" id="helpModal1">
    <div class="modal-backdrop" onclick="this.parentElement.classList.remove('active')"></div>
    <div class="modal-content">
        <button class="modal-close" onclick="this.parentElement.parentElement.classList.remove('active')">&times;</button>
        <h3>Business Report</h3>
        <p class="modal-subtitle">Detail Page Sales and Traffic by ASIN</p>
        <ol class="steps">
            <li>Open <strong>Seller Central</strong></li>
            <li>Go to <strong>Reports</strong> &rarr; <strong>Business Reports</strong></li>
            <li>In the left sidebar, click <strong>"Detail Page Sales and Traffic by Child Item"</strong></li>
            <li>Set the date range to <strong>Last 30 Days</strong> (or a full calendar month)</li>
            <li>Click <strong>Download (.csv)</strong></li>
        </ol>
        <div class="modal-note">This report contains your revenue, sessions, conversion rate, and Buy Box percentage per ASIN.</div>
    </div>
</div>

<div class="modal" id="helpModal2">
    <div class="modal-backdrop" onclick="this.parentElement.classList.remove('active')"></div>
    <div class="modal-content">
        <button class="modal-close" onclick="this.parentElement.parentElement.classList.remove('active')">&times;</button>
        <h3>Sponsored Products Search Term Report</h3>
        <p class="modal-subtitle">PPC advertising performance by search term</p>
        <ol class="steps">
            <li>Open <strong>Seller Central</strong></li>
            <li>Go to <strong>Advertising</strong> &rarr; <strong>Campaign Manager</strong></li>
            <li>Click the <strong>Reports</strong> tab (top navigation)</li>
            <li>Click <strong>Create Report</strong></li>
            <li>Report type: <strong>Sponsored Products</strong></li>
            <li>Report: <strong>Search Term</strong></li>
            <li>Time period: <strong>Last 30 Days</strong></li>
            <li>Click <strong>Run Report</strong>, then download when ready</li>
        </ol>
        <div class="modal-note">This report shows which search terms customers used, what you spent, and which terms converted into orders.</div>
    </div>
</div>

<div class="modal" id="helpModal3">
    <div class="modal-backdrop" onclick="this.parentElement.classList.remove('active')"></div>
    <div class="modal-content">
        <button class="modal-close" onclick="this.parentElement.parentElement.classList.remove('active')">&times;</button>
        <h3>FBA Inventory Health Report</h3>
        <p class="modal-subtitle">Stock levels, sell-through, and excess inventory</p>
        <ol class="steps">
            <li>Open <strong>Seller Central</strong></li>
            <li>Go to <strong>Inventory</strong> &rarr; <strong>Inventory Planning</strong></li>
            <li>Click <strong>Inventory Health</strong> (or <strong>FBA Inventory</strong>)</li>
            <li>Click <strong>Download</strong> (top right of the table)</li>
        </ol>
        <div class="modal-note">This report includes available units, weeks of cover, sell-through rates, and Amazon's own excess inventory flags.</div>
    </div>
</div>

<div class="modal" id="helpModal4">
    <div class="modal-backdrop" onclick="this.parentElement.classList.remove('active')"></div>
    <div class="modal-content">
        <button class="modal-close" onclick="this.parentElement.parentElement.classList.remove('active')">&times;</button>
        <h3>FBA Customer Returns Report</h3>
        <p class="modal-subtitle">Return reasons and customer comments</p>
        <ol class="steps">
            <li>Open <strong>Seller Central</strong></li>
            <li>Go to <strong>Reports</strong> &rarr; <strong>Fulfillment by Amazon</strong></li>
            <li>In the left sidebar under <strong>Customer Concessions</strong>, click <strong>FBA Customer Returns</strong></li>
            <li>Set the date range to <strong>Last 30 Days</strong></li>
            <li>Click <strong>Generate Report</strong>, then download when ready</li>
        </ol>
        <div class="modal-note">This report shows every return with the customer's stated reason and optional comments — critical for identifying listing or packaging issues.</div>
    </div>
</div>

<script>
// File upload preview + 4-file validation
document.getElementById('fileInput').addEventListener('change', function(e) {
    var list = document.getElementById('fileList');
    var btn = document.getElementById('submitBtn');
    var warn = document.getElementById('fileWarning');
    var count = e.target.files.length;

    list.innerHTML = '';
    for (var i = 0; i < count; i++) {
        var f = e.target.files[i];
        var size = (f.size / 1024).toFixed(0) + ' KB';
        list.innerHTML += '<div class="file-item"><span>' + f.name + '</span><span class="file-size">' + size + '</span></div>';
    }

    if (count >= 4) {
        btn.disabled = false;
        btn.textContent = 'Run Audit';
        warn.style.display = 'none';
    } else if (count > 0) {
        btn.disabled = true;
        btn.textContent = count + ' of 4 reports uploaded';
        warn.style.display = 'block';
    } else {
        btn.disabled = true;
        btn.textContent = 'Upload all 4 reports to start';
        warn.style.display = 'none';
    }
});

// Submit with progress overlay
document.getElementById('uploadForm').addEventListener('submit', function(e) {
    var files = document.getElementById('fileInput').files;
    if (!files.length) return;

    document.getElementById('submitBtn').disabled = true;
    document.getElementById('progressOverlay').classList.add('active');

    // Animate steps
    var steps = document.querySelectorAll('.progress-step');
    var timings = [0, 3000, 10000, 40000]; // when each step activates
    timings.forEach(function(t, i) {
        setTimeout(function() {
            if (i > 0) steps[i-1].classList.remove('active');
            steps[i].classList.add('active');
            steps[i].classList.add('done');
        }, t);
    });

    // Timeout warning after 50s
    setTimeout(function() {
        var warn = document.getElementById('progressWarn');
        if (warn) warn.style.display = 'block';
    }, 50000);

    // Hard timeout after 90s — show error
    setTimeout(function() {
        var overlay = document.getElementById('progressOverlay');
        if (overlay.classList.contains('active')) {
            overlay.innerHTML = '<div class="progress-box"><h2>This is taking longer than expected</h2><p style="color:#6b7280;margin:12px 0 24px;">The analysis may still be running. Please wait another minute or <a href="/" style="color:#2563eb;">try again</a>.</p></div>';
        }
    }, 90000);
});

// Close modal on Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(function(m) { m.classList.remove('active'); });
    }
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


PROCESSING_PAGE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Analyzing — Amazon Performance Audit</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
    background: #fafafa; color: #1a1a2e;
    display: flex; align-items: center; justify-content: center; min-height: 100vh;
    -webkit-font-smoothing: antialiased;
}
.box { text-align: center; max-width: 480px; width: 90%; }
.spinner {
    width: 48px; height: 48px;
    border: 3px solid #e5e7eb; border-top-color: #1a1a2e;
    border-radius: 50%; animation: spin 0.8s linear infinite;
    margin: 0 auto 24px;
}
@keyframes spin { to { transform: rotate(360deg); } }
h1 { font-size: 22px; margin-bottom: 6px; }
.sub { font-size: 14px; color: #9ca3af; margin-bottom: 32px; }

.steps { text-align: left; max-width: 380px; margin: 0 auto; }
.step {
    display: flex; gap: 14px; padding: 10px 0;
    opacity: 0.25; transition: opacity 0.4s;
}
.step.active, .step.done { opacity: 1; }
.step-dot {
    width: 24px; height: 24px; border-radius: 50%;
    border: 2px solid #d1d5db; flex-shrink: 0; margin-top: 1px;
    position: relative; transition: all 0.3s;
}
.step.active .step-dot {
    border-color: #2563eb; background: #2563eb;
    animation: pulse 1.5s ease-in-out infinite;
}
.step.active .step-dot::after {
    content: ''; position: absolute; inset: 4px;
    background: white; border-radius: 50%;
}
.step.done .step-dot {
    border-color: #059669; background: #059669;
}
.step.done .step-dot::after {
    content: '\\2713'; position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 12px; font-weight: bold;
}
@keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(37,99,235,0.3); }
    50% { box-shadow: 0 0 0 8px rgba(37,99,235,0); }
}
.step-label strong { display: block; font-size: 13px; }
.step-label span { font-size: 12px; color: #9ca3af; }

.elapsed { font-size: 12px; color: #d1d5db; margin-top: 20px; }
.error-box {
    background: #fef2f2; border: 1px solid #fecaca;
    color: #dc2626; padding: 16px; border-radius: 10px;
    margin-top: 16px; font-size: 13px; line-height: 1.5;
    text-align: left;
}
a.retry {
    display: inline-block; margin-top: 20px; padding: 12px 24px;
    background: #1a1a2e; color: white; text-decoration: none;
    border-radius: 8px; font-size: 14px; font-weight: 600;
}
</style>
</head><body>
<div class="box" id="content">
    <div class="spinner" id="spinner"></div>
    <h1>Analyzing your data</h1>
    <p class="sub">This may take a few minutes depending on data size</p>

    <div class="steps">
        <div class="step done" id="s1">
            <div class="step-dot"></div>
            <div class="step-label"><strong>Files uploaded</strong><span>Reports saved securely</span></div>
        </div>
        <div class="step" id="s2">
            <div class="step-dot"></div>
            <div class="step-label"><strong>Parsing & validating</strong><span>Detecting report types, checking structure</span></div>
        </div>
        <div class="step" id="s3">
            <div class="step-dot"></div>
            <div class="step-label"><strong>Cross-referencing data</strong><span>Aggregating at ASIN level across reports</span></div>
        </div>
        <div class="step" id="s4">
            <div class="step-dot"></div>
            <div class="step-label"><strong>Analysis</strong><span>Identifying findings, estimating impact</span></div>
        </div>
        <div class="step" id="s5">
            <div class="step-dot"></div>
            <div class="step-label"><strong>Building report</strong><span>Generating your performance audit</span></div>
        </div>
    </div>

    <div class="elapsed" id="elapsed"></div>
</div>
<script>
var auditId = "{{ audit_id }}";
var startTime = Date.now();

var stepMap = {
    'Starting': 's2',
    'Parsing files': 's2',
    'Cross-referencing data': 's3',
    'Analysis': 's4',
    'Building report': 's5',
    'Complete': 's5'
};
var stepOrder = ['s1','s2','s3','s4','s5'];
var currentStep = 's1';

function setStep(stepId) {
    // Mark all previous steps as done, current as active
    var reached = false;
    for (var i = 0; i < stepOrder.length; i++) {
        var el = document.getElementById(stepOrder[i]);
        if (stepOrder[i] === stepId) {
            reached = true;
            el.className = 'step active';
        } else if (!reached) {
            el.className = 'step done';
        } else {
            el.className = 'step';
        }
    }
    currentStep = stepId;
}

function poll() {
    var secs = Math.floor((Date.now() - startTime) / 1000);
    document.getElementById('elapsed').textContent = secs + 's elapsed';

    fetch('/api/status/' + auditId)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'done') {
                // Mark all done
                for (var i = 0; i < stepOrder.length; i++) {
                    document.getElementById(stepOrder[i]).className = 'step done';
                }
                document.getElementById('spinner').style.display = 'none';
                setTimeout(function() {
                    window.location.href = '/report/' + auditId + '/full';
                }, 800);
            } else if (data.status === 'error') {
                document.getElementById('spinner').style.display = 'none';
                document.getElementById('content').innerHTML =
                    '<h1>Analysis failed</h1>' +
                    '<div class="error-box">' + (data.error || 'Unknown error') + '</div>' +
                    '<a class="retry" href="/">Try Again</a>';
            } else {
                var target = stepMap[data.step] || currentStep;
                setStep(target);
                setTimeout(poll, 2000);
            }
        })
        .catch(function() {
            setTimeout(poll, 3000);
        });
}

setStep('s2');
setTimeout(poll, 1000);
</script>
</body></html>"""


# ─── ROUTES ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(UPLOAD_PAGE, error=None)


def process_audit_background(audit_id, saved_files):
    """Run the full audit pipeline in a background thread."""
    import time
    try:
        t0 = time.time()
        jobs[audit_id] = {"status": "processing", "step": "Parsing files", "error": None}

        # Parse and detect
        logger.info(f"[{audit_id}] Parsing and detecting report types...")
        reports = {}
        for filepath in saved_files:
            try:
                t1 = time.time()
                rtype, headers, rows = load_and_detect(filepath)
                if rtype:
                    max_rows = MAX_ROWS_FREE.get(rtype, 1000)
                    if len(rows) > max_rows:
                        rows = rows[:max_rows]
                    reports[rtype] = {"headers": headers, "rows": rows}
                    logger.info(f"[{audit_id}] Detected: {rtype} ({len(rows)} rows) in {time.time()-t1:.1f}s")
            except Exception as e:
                logger.error(f"[{audit_id}] Error parsing {filepath}: {e}")

        if not reports:
            jobs[audit_id] = {"status": "error", "step": "", "error": "No recognized Amazon reports found in the uploaded files."}
            return

        logger.info(f"[{audit_id}] Parsing done in {time.time()-t0:.1f}s total")

        # Summarize
        jobs[audit_id]["step"] = "Cross-referencing data"
        t2 = time.time()
        logger.info(f"[{audit_id}] Summarizing {len(reports)} reports...")
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

        flags = compute_cross_report_flags(biz_summary, ppc_summary, inv_summary, ret_summary)
        claude_input = build_claude_input(biz_summary, ppc_summary, inv_summary, ret_summary, flags, list(reports.keys()))
        logger.info(f"[{audit_id}] Summarization done in {time.time()-t2:.1f}s, input size: {len(claude_input)} chars")

        # Call Claude
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            jobs[audit_id] = {"status": "error", "step": "", "error": "ANTHROPIC_API_KEY not configured."}
            return

        jobs[audit_id]["step"] = "Analysis"
        t3 = time.time()
        logger.info(f"[{audit_id}] Calling Claude API...")
        audit_result = call_claude(claude_input)
        logger.info(f"[{audit_id}] Claude returned {len(audit_result.get('findings', []))} findings in {time.time()-t3:.1f}s")
        increment_daily_count()

        # Generate reports
        jobs[audit_id]["step"] = "Building report"
        html_full = generate_html(audit_result, summaries, is_paid=True)
        html_free = generate_html(audit_result, summaries, is_paid=False)

        OUTPUT_DIR.mkdir(exist_ok=True)
        with open(OUTPUT_DIR / f"{audit_id}_full.html", "w", encoding="utf-8") as f:
            f.write(html_full)
        with open(OUTPUT_DIR / f"{audit_id}_free.html", "w", encoding="utf-8") as f:
            f.write(html_free)
        with open(OUTPUT_DIR / f"{audit_id}_output.json", "w") as f:
            json.dump(audit_result, f, indent=2, default=str)

        jobs[audit_id] = {"status": "done", "step": "Complete", "error": None}
        logger.info(f"[{audit_id}] DONE")

    except Exception as e:
        logger.error(f"[{audit_id}] FATAL: {traceback.format_exc()}")
        jobs[audit_id] = {"status": "error", "step": "", "error": str(e)[:300]}

    finally:
        # Clean up uploaded files
        for fp in saved_files:
            try:
                os.remove(fp)
            except Exception:
                pass


@app.route("/upload", methods=["POST"])
def upload():
    if not check_daily_limit():
        return render_template_string(ERROR_PAGE,
            title="Daily limit reached",
            message="We've hit the daily audit limit for this test environment. Try again tomorrow."
        ), 429

    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return render_template_string(UPLOAD_PAGE, error="Please select at least one file.")

    audit_id = str(uuid.uuid4())[:8]
    audit_dir = UPLOAD_DIR / audit_id
    audit_dir.mkdir(parents=True, exist_ok=True)

    # Save files immediately (fast — no timeout risk)
    saved = []
    for f in files:
        if f.filename:
            safe_name = f"{uuid.uuid4().hex[:8]}_{Path(f.filename).suffix}"
            filepath = audit_dir / safe_name
            f.save(str(filepath))
            saved.append(str(filepath))
            logger.info(f"[{audit_id}] Saved: {f.filename}")

    if not saved:
        return render_template_string(UPLOAD_PAGE, error="No valid files uploaded.")

    if len(saved) < 4:
        shutil.rmtree(audit_dir, ignore_errors=True)
        return render_template_string(UPLOAD_PAGE,
            error=f"Please upload all 4 reports. You uploaded {len(saved)} file(s). Click the report cards to see where to download each one.")

    # Start processing in background thread (avoids Railway timeout)
    jobs[audit_id] = {"status": "processing", "step": "Starting", "error": None}
    thread = threading.Thread(target=process_audit_background, args=(audit_id, saved))
    thread.daemon = True
    thread.start()

    # Redirect to polling page immediately (fast response = no timeout)
    return redirect(url_for("processing_page", audit_id=audit_id))


@app.route("/processing/<audit_id>")
def processing_page(audit_id):
    """Page that polls for job completion."""
    return render_template_string(PROCESSING_PAGE, audit_id=audit_id)


@app.route("/api/status/<audit_id>")
def job_status(audit_id):
    """API endpoint for polling job status."""
    job = jobs.get(audit_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    return jsonify(job)


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

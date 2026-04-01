"""
Amazon Watchdog — Premium HTML Report Generator (Amazon Performance Audit)
Apple-inspired design. Opens in browser, prints to PDF.
"""

import json
from datetime import datetime


def _score_gradient(score):
    if score >= 71: return "linear-gradient(135deg, #34d399, #059669)"
    if score >= 51: return "linear-gradient(135deg, #fbbf24, #d97706)"
    if score >= 31: return "linear-gradient(135deg, #f97316, #dc2626)"
    return "linear-gradient(135deg, #ef4444, #991b1b)"


def _score_label(score):
    if score >= 71: return "Solid Performance"
    if score >= 51: return "Room for Improvement"
    if score >= 31: return "Clear Deficiencies"
    return "Significant Issues"


def _sev_class(sev):
    return {"critical": "critical", "warning": "warning", "info": "info"}.get(sev, "info")


def _sev_icon(sev):
    return {"critical": "&#xf06a;", "warning": "&#xf071;", "info": "&#xf05a;"}.get(sev, "&#xf05a;")


LOGIC_MAP = {
    "buy box": (
        "We cross-referenced the Featured Offer (Buy Box) percentage from the Business Report "
        "with each ASIN's revenue and conversion data. When Buy Box share drops below 85%, "
        "Amazon shows competitor offers to the majority of visitors. The conversion rate proves "
        "demand exists — revenue loss is proportional to Buy Box loss percentage."
    ),
    "return rate": (
        "Return reasons from the FBA Customer Returns Report were classified into quality-related "
        "(NOT_COMPATIBLE, QUALITY_UNACCEPTABLE, DEFECTIVE) and non-quality categories. The rate was "
        "benchmarked against the industry-standard healthy threshold of &lt;1.5%. High quality-return "
        "rates can trigger Amazon listing suppression and negatively impact organic ranking."
    ),
    "ppc": (
        "Ad spend from the Search Term Report was measured against total revenue from the Business Report. "
        "Campaign structures and match type distribution were analyzed. For low-price products (sub-EUR 10), "
        "CPC sensitivity is critical — small bid increases disproportionately erode margins."
    ),
    "search term harvesting": (
        "Auto campaigns serve as keyword discovery mechanisms. We analyzed the Search Term Report "
        "for proven conversions at low CPC. For products priced under EUR 10, the math is tight: "
        "at CPC EUR 0.15 with 8 clicks/order, cost-per-acquisition is EUR 1.20 (12% ACOS). "
        "Doubling CPC to EUR 0.30 makes the same term unprofitable. Exact Match campaigns on "
        "proven low-CPC terms provide the bid control needed to protect these margins."
    ),
    "wasted": (
        "The Search Term Report was filtered for entries with 10+ clicks and zero conversions. "
        "Each non-converting click represents direct budget waste. These terms should be managed "
        "as negative keywords — a fundamental PPC hygiene task."
    ),
    "stock": (
        "Weeks-of-cover from the Inventory Health Report (trailing 30-day sell-through) was "
        "cross-referenced with revenue data. Products below 2 weeks of cover risk stockout, "
        "causing immediate revenue loss and 4-8 weeks of organic ranking recovery time."
    ),
    "exact match": (
        "High-converting ASINs (&gt;8% conversion) from the Business Report were matched against "
        "campaign structures in the Search Term Report. Exact Match campaigns provide the highest "
        "bid control and typically deliver 20-40% lower ACOS than Auto/Broad campaigns."
    ),
    "excess": (
        "SKUs with &gt;26 weeks of cover were flagged from the Inventory Health Report. "
        "After 181 days, Amazon applies aged inventory surcharges. After 365 days, long-term "
        "storage fees increase substantially, eroding margins."
    ),
    "variation": (
        "Return reasons were cross-referenced with the product's variation structure from the "
        "Business Report. 'UNWANTED_ITEM' combined with low Buy Box often indicates customer "
        "confusion about product options or misleading listing content."
    ),
    "organic": (
        "Organic vs. paid revenue split was calculated by comparing Search Term Report attributed "
        "sales against total Business Report revenue. A &gt;90% organic share, while positive for "
        "margins, raises questions about active agency contribution and management value."
    ),
}


def _get_logic(title):
    t = title.lower()
    for key, text in LOGIC_MAP.items():
        if key in t:
            return text
    return (
        "This finding was derived by cross-referencing multiple Seller Central data sources "
        "and comparing against established Amazon marketplace performance benchmarks."
    )


def generate_html(audit_result, summaries, is_paid=True):
    score = audit_result.get("overallScore", 0)
    findings = audit_result.get("findings", [])
    recs = audit_result.get("recommendations", [])
    summary = audit_result.get("executiveSummary", "")
    dq_notes = audit_result.get("dataQualityNotes", [])

    biz = summaries.get("business_report")
    ppc = summaries.get("search_term_report")
    inv = summaries.get("inventory_health")
    ret = summaries.get("customer_returns")

    crit = sum(1 for f in findings if f.get("severity") == "critical")
    warn = sum(1 for f in findings if f.get("severity") == "warning")
    info_count = sum(1 for f in findings if f.get("severity") == "info")

    # Build metrics
    metrics_html = ""
    metric_items = []
    if biz:
        metric_items.append(("EUR {:,.0f}".format(biz["totalRevenue"]), "Total Revenue", "revenue"))
        metric_items.append(("{:,}".format(biz["totalUnits"]), "Units Ordered", "units"))
        metric_items.append(("{:,}".format(biz["totalSessions"]), "Sessions", "sessions"))
    if ppc:
        metric_items.append(("EUR {:,.0f}".format(ppc["totalSpend"]), "Ad Spend", "spend"))
        metric_items.append(("{:.1f}%".format(ppc["overallAcos"]), "Overall ACOS", "acos"))
        metric_items.append(("EUR {:,.0f}".format(ppc["totalSales"]), "Ad Sales", "sales"))
    if ret:
        metric_items.append(("{:,}".format(ret["totalUnitsReturned"]), "Returns", "returns"))
        metric_items.append(("{:.1f}%".format(ret["notAsDescribedRate"]), "Quality Issue Rate", "quality"))
    if inv:
        metric_items.append(("{:,}".format(inv["totalSkus"]), "Active SKUs", "skus"))

    for val, label, cls in metric_items:
        metrics_html += f"""
        <div class="metric-card">
            <div class="metric-value">{val}</div>
            <div class="metric-label">{label}</div>
        </div>"""

    # Findings overview pills
    overview_html = ""
    for f in findings:
        sev = f.get("severity", "info")
        impact = f.get("estimatedImpactEur")
        impact_type = f.get("impactType", "risk")
        if impact:
            if impact_type == "potential_uplift":
                impact_str = '<span class="impact-uplift">&#9650; EUR {:,.0f}/mo</span>'.format(impact)
            elif impact_type == "cost_savings":
                impact_str = '<span class="impact-save">&#9660; EUR {:,.0f}/mo</span>'.format(impact)
            else:
                impact_str = '<span class="impact-risk">EUR {:,.0f}/mo</span>'.format(impact)
        else:
            impact_str = ""
        overview_html += f"""
        <div class="overview-row {_sev_class(sev)}">
            <span class="overview-id">{f.get('id', '')}</span>
            <span class="overview-badge {_sev_class(sev)}">{sev.upper()}</span>
            <span class="overview-title">{f.get('title', '')}</span>
            <span class="overview-impact">{impact_str}</span>
        </div>"""

    # Individual findings
    findings_html = ""
    for i, f in enumerate(findings):
        if not is_paid:
            # Free tier: show NO full finding cards — just the overview table + paywall
            findings_html = f"""
            <div class="paywall-gate">
                <div class="paywall-lock">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                        <path d="M7 11V7a5 5 0 0110 0v4"/>
                    </svg>
                </div>
                <h3>{len(findings)} findings identified</h3>
                <p>Your audit found {crit} critical issues, {warn} warnings, and {info_count} informational items.
                Unlock the full report to see detailed analysis, data evidence, reasoning, and action steps for each finding.</p>
                <a href="#" class="paywall-cta">Unlock Full Report — EUR 9</a>
                <div class="paywall-sub">One-time purchase. No subscription required.</div>
                <div class="paywall-teaser">Want weekly automated audits? <strong>Subscribe for EUR 29/month</strong> — coming soon with full Seller Central integration.</div>
            </div>"""
            break

        sev = f.get("severity", "info")
        meta_cls = _sev_class(sev)
        impact = f.get("estimatedImpactEur")
        data_points = f.get("dataPoints", [])
        logic = _get_logic(f.get("title", ""))

        dp_html = ""
        if data_points:
            dp_rows = ""
            for dp in data_points:
                dp_rows += f"""
                <tr>
                    <td class="dp-label">{dp.get('label', '')}</td>
                    <td class="dp-value">{dp.get('value', '')}</td>
                    <td class="dp-context">{dp.get('context', '')}</td>
                </tr>"""
            dp_html = f"""
            <div class="data-table-wrap">
                <table class="data-table">
                    <thead><tr><th>Metric</th><th>Value</th><th>Benchmark / Context</th></tr></thead>
                    <tbody>{dp_rows}</tbody>
                </table>
            </div>"""

        impact_html = ""
        impact_type = f.get("impactType", "risk")
        if impact:
            if impact_type == "potential_uplift":
                impact_cls = "uplift"
                impact_label = "potential monthly uplift"
            elif impact_type == "cost_savings":
                impact_cls = "savings"
                impact_label = "potential monthly savings"
            else:
                impact_cls = "risk"
                impact_label = "estimated monthly risk"
            impact_html = f"""
            <div class="impact-badge {impact_cls}">
                <span class="impact-amount">EUR {impact:,.0f}</span>
                <span class="impact-period">{impact_label}</span>
            </div>"""

        action = f.get("actionItem", "")
        action_html = ""
        if action:
            action_html = f"""
            <div class="action-box">
                <div class="action-label">Recommended Action</div>
                <div class="action-text">"{action}"</div>
            </div>"""

        findings_html += f"""
        <article class="finding-card {meta_cls}">
            <div class="finding-header">
                <div class="finding-header-left">
                    <span class="finding-id">{f.get('id', '')}</span>
                    <span class="severity-pill {meta_cls}">{sev.upper()}</span>
                </div>
                {impact_html}
            </div>
            <h3 class="finding-title">{f.get('title', '')}</h3>
            <p class="finding-desc">{f.get('description', '')}</p>
            <div class="logic-block">
                <div class="logic-label">Analytical Reasoning</div>
                <p>{logic}</p>
            </div>
            {dp_html}
            {action_html}
        </article>"""

    # Recommendations
    recs_html = ""
    if is_paid and recs:
        for rec in recs:
            savings = rec.get("estimatedSavingsEur")
            rec_impact_type = rec.get("impactType", "potential_uplift")
            savings_html = ""
            if savings:
                if rec_impact_type == "cost_savings":
                    savings_html = f'<div class="rec-savings save">Potential savings: EUR {savings:,.0f}/month</div>'
                else:
                    savings_html = f'<div class="rec-savings uplift">Potential uplift: EUR {savings:,.0f}/month</div>'
            recs_html += f"""
            <div class="rec-card">
                <div class="rec-number">{rec.get('priority', '')}</div>
                <div class="rec-content">
                    <h4>{rec.get('title', '')}</h4>
                    <p>{rec.get('description', '')}</p>
                    {savings_html}
                </div>
            </div>"""

    dq_html = ""
    for note in dq_notes:
        dq_html += f"<li>{note}</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Amazon Watchdog — Performance Audit</title>
<style>
/* ═══ RESET & BASE ═══ */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
    --ink: #1a1a2e;
    --ink-light: #6b7280;
    --ink-muted: #9ca3af;
    --bg: #fafafa;
    --white: #ffffff;
    --surface: #ffffff;
    --border: rgba(0,0,0,0.06);
    --border-strong: rgba(0,0,0,0.1);
    --critical: #dc2626;
    --critical-bg: #fef2f2;
    --critical-border: #fecaca;
    --warning: #d97706;
    --warning-bg: #fffbeb;
    --warning-border: #fde68a;
    --info: #2563eb;
    --info-bg: #eff6ff;
    --info-border: #bfdbfe;
    --green: #059669;
    --green-bg: #ecfdf5;
    --radius: 16px;
    --radius-sm: 10px;
    --radius-xs: 6px;
    --shadow-sm: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.03);
    --shadow: 0 4px 24px rgba(0,0,0,0.06);
    --shadow-lg: 0 12px 48px rgba(0,0,0,0.08);
}}

body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text',
                 'Helvetica Neue', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--ink);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}}

/* ═══ LAYOUT ═══ */
.container {{
    max-width: 860px;
    margin: 0 auto;
    padding: 0 24px;
}}

section {{
    margin-bottom: 64px;
}}

/* ═══ COVER / HERO ═══ */
.hero {{
    padding: 80px 0 60px;
    text-align: center;
    position: relative;
}}

.hero::after {{
    content: '';
    position: absolute;
    bottom: 0;
    left: 50%;
    transform: translateX(-50%);
    width: 80px;
    height: 1px;
    background: var(--border-strong);
}}

.brand {{
    font-size: 11px;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: var(--ink-muted);
    margin-bottom: 16px;
    font-weight: 500;
}}

.hero h1 {{
    font-size: 44px;
    font-weight: 700;
    letter-spacing: -1.5px;
    line-height: 1.15;
    margin-bottom: 12px;
    background: linear-gradient(135deg, var(--ink) 0%, #374151 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}}

.hero .subtitle {{
    font-size: 17px;
    color: var(--ink-light);
    font-weight: 400;
    margin-bottom: 48px;
}}

/* ═══ SCORE ═══ */
.score-container {{
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
}}

.score-ring {{
    width: 140px;
    height: 140px;
    border-radius: 50%;
    background: {_score_gradient(score)};
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 8px 32px rgba(0,0,0,0.12);
    position: relative;
}}

.score-ring::before {{
    content: '';
    position: absolute;
    inset: 6px;
    border-radius: 50%;
    background: var(--white);
}}

.score-number {{
    position: relative;
    z-index: 1;
    font-size: 48px;
    font-weight: 700;
    color: var(--ink);
    line-height: 1;
}}

.score-number span {{
    font-size: 20px;
    color: var(--ink-muted);
    font-weight: 400;
}}

.score-label {{
    font-size: 15px;
    font-weight: 600;
    color: var(--ink-light);
}}

.score-breakdown {{
    display: flex;
    gap: 20px;
    margin-top: 8px;
    font-size: 13px;
    color: var(--ink-muted);
}}

.score-breakdown .dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    margin-right: 4px;
    vertical-align: middle;
}}

.dot.crit {{ background: var(--critical); }}
.dot.warn {{ background: var(--warning); }}
.dot.inf {{ background: var(--info); }}

.date {{
    font-size: 13px;
    color: var(--ink-muted);
    margin-top: 32px;
}}

/* ═══ METRICS GRID ═══ */
.metrics-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin: 48px 0;
}}

.metric-card {{
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 20px 16px;
    text-align: center;
    transition: box-shadow 0.2s ease;
}}

.metric-card:hover {{
    box-shadow: var(--shadow);
}}

.metric-value {{
    font-size: 22px;
    font-weight: 700;
    color: var(--ink);
    letter-spacing: -0.5px;
    margin-bottom: 4px;
}}

.metric-label {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--ink-muted);
    font-weight: 500;
}}

/* ═══ SECTION HEADERS ═══ */
.section-header {{
    margin-bottom: 32px;
}}

.section-number {{
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 3px;
    color: var(--info);
    text-transform: uppercase;
    margin-bottom: 8px;
}}

.section-header h2 {{
    font-size: 32px;
    font-weight: 700;
    letter-spacing: -0.8px;
    color: var(--ink);
}}

.section-divider {{
    width: 40px;
    height: 3px;
    background: var(--ink);
    margin-top: 16px;
    border-radius: 2px;
}}

/* ═══ EXECUTIVE SUMMARY ═══ */
.exec-summary {{
    font-size: 16px;
    line-height: 1.75;
    color: var(--ink-light);
    max-width: 720px;
}}

/* ═══ METHODOLOGY ═══ */
.method-grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
    margin-top: 24px;
}}

.method-card {{
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 20px;
}}

.method-card h4 {{
    font-size: 13px;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 8px;
}}

.method-card p, .method-card li {{
    font-size: 13px;
    color: var(--ink-light);
    line-height: 1.6;
}}

.method-card ul {{
    list-style: none;
    padding: 0;
}}

.method-card li::before {{
    content: '\\2022';
    color: var(--info);
    font-weight: bold;
    margin-right: 8px;
}}

.dq-notes {{
    margin-top: 24px;
    padding: 16px 20px;
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-size: 12px;
    color: var(--ink-muted);
}}

.dq-notes li {{
    margin-bottom: 4px;
    line-height: 1.5;
}}

/* ═══ FINDINGS OVERVIEW TABLE ═══ */
.overview-table {{
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 48px;
    box-shadow: var(--shadow-sm);
}}

.overview-row {{
    display: grid;
    grid-template-columns: 52px 80px 1fr 120px;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    transition: background 0.15s ease;
}}

.overview-row:last-child {{ border-bottom: none; }}
.overview-row:hover {{ background: var(--bg); }}

.overview-id {{
    font-weight: 700;
    font-size: 12px;
    color: var(--ink-muted);
}}

.severity-pill, .overview-badge {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.5px;
    padding: 3px 8px;
    border-radius: 4px;
    text-align: center;
}}

.severity-pill.critical, .overview-badge.critical {{ background: var(--critical-bg); color: var(--critical); }}
.severity-pill.warning, .overview-badge.warning {{ background: var(--warning-bg); color: var(--warning); }}
.severity-pill.info, .overview-badge.info {{ background: var(--info-bg); color: var(--info); }}

.overview-title {{
    font-weight: 500;
    color: var(--ink);
    padding: 0 12px;
}}

.overview-impact {{
    text-align: right;
    font-weight: 600;
    font-size: 13px;
}}

.impact-uplift {{ color: var(--green); }}
.impact-save {{ color: var(--warning); }}
.impact-risk {{ color: var(--critical); }}

/* ═══ FINDING CARDS ═══ */
.finding-card {{
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 32px;
    margin-bottom: 24px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s ease;
    position: relative;
    overflow: hidden;
}}

.finding-card::before {{
    content: '';
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: 4px;
}}

.finding-card.critical::before {{ background: var(--critical); }}
.finding-card.warning::before {{ background: var(--warning); }}
.finding-card.info::before {{ background: var(--info); }}

.finding-card:hover {{ box-shadow: var(--shadow); }}

.finding-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 12px;
}}

.finding-header-left {{
    display: flex;
    align-items: center;
    gap: 10px;
}}

.finding-id {{
    font-size: 12px;
    font-weight: 700;
    color: var(--ink-muted);
    background: var(--bg);
    padding: 2px 8px;
    border-radius: 4px;
}}

.finding-title {{
    font-size: 20px;
    font-weight: 700;
    color: var(--ink);
    letter-spacing: -0.3px;
    margin-bottom: 12px;
    line-height: 1.3;
}}

.finding-desc {{
    font-size: 14px;
    line-height: 1.7;
    color: var(--ink-light);
    margin-bottom: 16px;
}}

.impact-badge {{
    text-align: right;
    flex-shrink: 0;
}}

.impact-amount {{
    display: block;
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.5px;
    line-height: 1.2;
}}

.impact-badge.uplift .impact-amount {{ color: var(--green); }}
.impact-badge.savings .impact-amount {{ color: var(--warning); }}
.impact-badge.risk .impact-amount {{ color: var(--critical); }}

.impact-period {{
    font-size: 10px;
    color: var(--ink-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

/* ═══ LOGIC BLOCK ═══ */
.logic-block {{
    background: var(--bg);
    border-left: 3px solid var(--border-strong);
    border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
    padding: 16px 20px;
    margin-bottom: 16px;
}}

.logic-label {{
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--ink-muted);
    margin-bottom: 6px;
}}

.logic-block p {{
    font-size: 13px;
    line-height: 1.65;
    color: var(--ink-light);
    font-style: italic;
}}

/* ═══ DATA TABLE ═══ */
.data-table-wrap {{
    overflow-x: auto;
    margin-bottom: 16px;
    border-radius: var(--radius-xs);
    border: 1px solid var(--border);
}}

.data-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
}}

.data-table thead {{
    background: var(--ink);
}}

.data-table th {{
    color: white;
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    padding: 10px 14px;
    text-align: left;
}}

.data-table td {{
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
}}

.data-table tr:nth-child(even) {{ background: var(--bg); }}
.data-table tr:last-child td {{ border-bottom: none; }}

.dp-label {{ font-weight: 600; color: var(--ink); white-space: nowrap; }}
.dp-value {{ font-weight: 700; color: var(--ink); }}
.dp-context {{ color: var(--ink-muted); font-size: 12px; }}

/* ═══ ACTION BOX ═══ */
.action-box {{
    background: linear-gradient(135deg, #eff6ff, #f0f9ff);
    border: 1px solid var(--info-border);
    border-radius: var(--radius-sm);
    padding: 20px 24px;
}}

.action-label {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--info);
    margin-bottom: 8px;
}}

.action-text {{
    font-size: 14px;
    line-height: 1.6;
    color: var(--ink);
    font-style: italic;
}}

/* ═══ RECOMMENDATIONS ═══ */
.rec-card {{
    display: flex;
    gap: 20px;
    background: var(--white);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 24px;
    margin-bottom: 16px;
    box-shadow: var(--shadow-sm);
}}

.rec-number {{
    font-size: 32px;
    font-weight: 700;
    color: var(--info);
    opacity: 0.3;
    line-height: 1;
    flex-shrink: 0;
    width: 40px;
}}

.rec-content h4 {{
    font-size: 15px;
    font-weight: 700;
    color: var(--ink);
    margin-bottom: 6px;
}}

.rec-content p {{
    font-size: 13px;
    color: var(--ink-light);
    line-height: 1.6;
}}

.rec-savings {{
    margin-top: 8px;
    font-size: 13px;
    font-weight: 600;
}}

.rec-savings.uplift {{ color: var(--green); }}
.rec-savings.save {{ color: var(--warning); }}

/* ═══ PAYWALL ═══ */
.paywall-gate {{
    text-align: center;
    padding: 60px 40px;
    background: linear-gradient(180deg, var(--white) 0%, var(--bg) 100%);
    border: 2px dashed var(--border-strong);
    border-radius: var(--radius);
    margin: 32px 0;
}}

.paywall-lock {{
    color: var(--ink-muted);
    margin-bottom: 16px;
}}

.paywall-gate h3 {{
    font-size: 22px;
    font-weight: 700;
    margin-bottom: 8px;
}}

.paywall-gate p {{
    font-size: 14px;
    color: var(--ink-light);
    max-width: 400px;
    margin: 0 auto 24px;
}}

.paywall-cta {{
    display: inline-block;
    background: var(--ink);
    color: white;
    padding: 14px 32px;
    border-radius: 100px;
    font-size: 15px;
    font-weight: 600;
    text-decoration: none;
    transition: all 0.2s ease;
}}

.paywall-cta:hover {{
    background: #374151;
    transform: translateY(-1px);
    box-shadow: var(--shadow);
}}

.paywall-sub {{
    font-size: 13px;
    color: var(--ink-muted);
    margin-top: 12px;
}}

.paywall-teaser {{
    font-size: 13px;
    color: var(--ink-light);
    margin-top: 20px;
    padding-top: 20px;
    border-top: 1px solid var(--border);
    max-width: 400px;
    margin-left: auto;
    margin-right: auto;
}}

/* ═══ FOOTER ═══ */
.footer {{
    padding: 40px 0;
    border-top: 1px solid var(--border);
    text-align: center;
    font-size: 12px;
    color: var(--ink-muted);
    line-height: 1.8;
}}

.footer strong {{
    color: var(--ink);
}}

/* ═══ PRINT STYLES ═══ */
@media print {{
    body {{ background: white; }}
    .container {{ max-width: 100%; }}
    .finding-card, .rec-card, .metric-card {{ break-inside: avoid; }}
    .finding-card:hover, .metric-card:hover {{ box-shadow: none; }}
    section {{ margin-bottom: 32px; }}
    .hero {{ padding: 40px 0 30px; }}
    @page {{ margin: 15mm; size: A4; }}
}}
</style>
</head>
<body>

<!-- HERO / COVER -->
<section class="hero">
<div class="container">
    <div class="brand">Amazon Watchdog</div>
    <h1>Amazon Performance<br>Audit</h1>
    <p class="subtitle">Independent analysis of your Seller Central data</p>

    <div class="score-container">
        <div class="score-ring">
            <div class="score-number">{score}<span>/100</span></div>
        </div>
        <div class="score-label">{_score_label(score)}</div>
        <div class="score-breakdown">
            <span><span class="dot crit"></span>{crit} Critical</span>
            <span><span class="dot warn"></span>{warn} Warnings</span>
            <span><span class="dot inf"></span>{info_count} Info</span>
        </div>
    </div>

    <div class="date">{datetime.now().strftime('%B %d, %Y')}</div>
</div>
</section>

<!-- METRICS -->
<section>
<div class="container">
    <div class="metrics-grid">{metrics_html}
    </div>
</div>
</section>

<!-- EXECUTIVE SUMMARY -->
<section>
<div class="container">
    <div class="section-header">
        <div class="section-number">01 &mdash; Overview</div>
        <h2>Executive Summary</h2>
        <div class="section-divider"></div>
    </div>
    <p class="exec-summary">{summary}</p>
</div>
</section>

<!-- METHODOLOGY -->
<section>
<div class="container">
    <div class="section-header">
        <div class="section-number">02 &mdash; Approach</div>
        <h2>Methodology</h2>
        <div class="section-divider"></div>
    </div>
    <div class="method-grid">
        <div class="method-card">
            <h4>Cross-Report Analysis</h4>
            <p>Findings are derived by joining data across multiple reports using ASIN as the common key. Patterns that are invisible in any single report become clear when data is correlated.</p>
        </div>
        <div class="method-card">
            <h4>Industry Benchmarks</h4>
            <p>Each metric is compared against established Amazon marketplace thresholds: Buy Box &gt;85%, return quality rate &lt;1.5%, inventory cover 4-12 weeks, and category-specific ACOS targets.</p>
        </div>
        <div class="method-card">
            <h4>Data Sources</h4>
            <ul>
                <li>Business Report (Sales &amp; Traffic)</li>
                <li>Sponsored Products Search Terms</li>
                <li>FBA Inventory Health</li>
                <li>FBA Customer Returns</li>
            </ul>
        </div>
        <div class="method-card">
            <h4>Scoring Model</h4>
            <p>0-30: Severe gaps &bull; 31-50: Clear deficiencies &bull; 51-70: Improvement needed &bull; 71-85: Solid &bull; 86-100: Excellent. Critical findings weigh 3x.</p>
        </div>
    </div>
    {"<div class='dq-notes'><strong>Data quality notes</strong><ul>" + dq_html + "</ul></div>" if dq_html else ""}
</div>
</section>

<!-- FINDINGS -->
<section>
<div class="container">
    <div class="section-header">
        <div class="section-number">03 &mdash; Analysis</div>
        <h2>Findings</h2>
        <div class="section-divider"></div>
    </div>

    <div class="overview-table">{overview_html}
    </div>

    {findings_html}
</div>
</section>

<!-- RECOMMENDATIONS -->
{"<section><div class='container'><div class='section-header'><div class='section-number'>04 &mdash; Next Steps</div><h2>Prioritized Recommendations</h2><div class='section-divider'></div></div>" + recs_html + "</div></section>" if is_paid and recs_html else ""}

<!-- FOOTER -->
<footer class="footer">
<div class="container">
    <p>This analysis is based on the data you provided and does not constitute financial or business advice.<br>
    Estimated impacts are directional projections — actual results may vary. Your data was deleted immediately after processing.</p>
    <p style="margin-top: 12px;"><strong>Amazon Watchdog</strong> &middot; amazonwatchdog.com</p>
    <p style="margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); font-size: 11px;">
    Coming soon: <strong>Fully automated weekly audits</strong> — connect your Seller Central account and receive performance reports automatically. No more manual downloads.</p>
</div>
</footer>

</body>
</html>"""


def save_html_report(audit_result, summaries, output_path, is_paid=True):
    html = generate_html(audit_result, summaries, is_paid)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML saved: {output_path}")

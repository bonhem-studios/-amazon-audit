#!/usr/bin/env python3
"""
Amazon Performance Audit — Local Audit Pipeline
Reads 4 Amazon Seller Central reports, summarizes, calls Claude, generates PDF.
"""

import csv
import json
import os
import sys
import re
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

import openpyxl
from anthropic import Anthropic
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ─── CONFIG ────────────────────────────────────────────────────────────────────

FREE_TIER_MAX_FINDINGS = 3
SAMPLE_DATA_DIR = Path(__file__).parent / "sample-data"
OUTPUT_DIR = Path(__file__).parent / "output"

# ─── REPORT DETECTION (by column structure, NOT filename) ──────────────────────

# Signature columns that uniquely identify each report type
REPORT_SIGNATURES = {
    "business_report": {
        "required": ["(child) asin", "sessions", "buy box", "units ordered",
                     "ordered product sales", "unit session percentage"],
        "alt_required": ["(child) asin", "session", "featured offer", "units ordered",
                        "ordered product sales", "unit session"],
    },
    "search_term_report": {
        "required": ["campaign name", "customer search term", "impressions",
                     "clicks", "spend"],
        "alt_required": ["campaign", "search term", "impressions", "clicks", "spend"],
    },
    "inventory_health": {
        "required": ["asin", "available", "weeks-of-cover", "estimated-excess"],
        "alt_required": ["asin", "available", "weeks-of-cover-t30",
                        "units-shipped-t30"],
    },
    "customer_returns": {
        "required": ["return-date", "asin", "quantity", "reason",
                     "detailed-disposition"],
        "alt_required": ["return-date", "order-id", "asin", "reason"],
    },
}


def normalize_header(h: str) -> str:
    """Lowercase, strip quotes/whitespace for matching."""
    return h.strip().strip('"').lower()


def detect_report_type_from_headers(headers: list) -> str:
    """Detect report type by matching column headers against signatures."""
    norm = [normalize_header(h) for h in headers]
    joined = " | ".join(norm)

    best_match = None
    best_score = 0

    for rtype, sigs in REPORT_SIGNATURES.items():
        for sig_key in ["required", "alt_required"]:
            sig_cols = sigs[sig_key]
            score = sum(1 for sc in sig_cols if any(sc in nh for nh in norm))
            if score > best_score:
                best_score = score
                best_match = rtype

    return best_match if best_score >= 3 else None


# ─── PARSERS ───────────────────────────────────────────────────────────────────

def parse_euro(val: str) -> float:
    """Parse European currency: '€41,139.02' or '41.139,02' → float."""
    if not val or val == "--" or val == "-":
        return 0.0
    s = str(val).strip().replace("€", "").replace("$", "").strip()
    # If format is "12,796" (comma as thousands) — US/mixed format
    # If format is "12.796,02" (dot thousands, comma decimal) — pure EU
    if "," in s and "." in s:
        if s.rindex(",") > s.rindex("."):
            # EU: 12.796,02
            s = s.replace(".", "").replace(",", ".")
        else:
            # US: 12,796.02
            s = s.replace(",", "")
    elif "," in s:
        # Could be "12,796" (thousands) or "12,02" (decimal)
        parts = s.split(",")
        if len(parts[-1]) == 3:
            s = s.replace(",", "")  # thousands separator
        else:
            s = s.replace(",", ".")  # decimal
    return float(s) if s else 0.0


def parse_pct(val: str) -> float:
    """Parse percentage: '36.03%' → 36.03."""
    if not val or val == "--" or val == "-":
        return 0.0
    s = str(val).strip().replace("%", "").replace(",", ".").strip()
    return float(s) if s else 0.0


def parse_int_str(val: str) -> int:
    """Parse integer with possible comma thousands: '12,796' → 12796."""
    if not val or val == "--" or val == "-":
        return 0
    s = str(val).strip().replace('"', '').replace(",", "").replace(".", "")
    try:
        return int(float(s))
    except ValueError:
        return 0


def repair_asin(val: str) -> str:
    """Recover ASIN from scientific notation (e.g. 3.1E+09)."""
    if not val:
        return val
    s = str(val).strip()
    if re.match(r'^\d+\.?\d*[eE]\+?\d+$', s):
        num = float(s)
        return str(int(round(num))).zfill(10)
    return s


def extract_asin_from_campaign(campaign_name: str) -> str:
    """Extract ASIN from campaign name like 'DE_Bali Curls_SP_RCH1_Auto_Curl Volume Foam_B0DRDF8SQR'."""
    match = re.search(r'\b(B0[A-Z0-9]{8,})\b', campaign_name)
    return match.group(1) if match else None


def read_csv_file(filepath: str) -> tuple[list, list]:
    """Read CSV, return (headers, rows_as_dicts)."""
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    # Detect encoding issues (mojibake)
    if "Ã¼" in content or "Ã¶" in content or "Ã¤" in content:
        with open(filepath, "r", encoding="latin-1") as f:
            content = f.read()

    reader = csv.DictReader(content.splitlines())
    headers = reader.fieldnames or []
    rows = list(reader)
    return headers, rows


def read_xlsx_file(filepath: str) -> tuple[list, list]:
    """Read XLSX, return (headers, rows_as_dicts)."""
    wb = openpyxl.load_workbook(filepath, read_only=False)
    ws = wb.active
    rows_raw = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows_raw:
        return [], []

    headers = [str(h) if h else f"col_{i}" for i, h in enumerate(rows_raw[0])]
    rows = []
    for row in rows_raw[1:]:
        d = {}
        for i, val in enumerate(row):
            if i < len(headers):
                d[headers[i]] = val
        rows.append(d)
    return headers, rows


def load_and_detect(filepath: str) -> tuple[str, list, list]:
    """Load file (CSV or XLSX), auto-detect report type, return (type, headers, rows)."""
    ext = Path(filepath).suffix.lower()
    if ext in (".xlsx", ".xls"):
        headers, rows = read_xlsx_file(filepath)
    else:
        headers, rows = read_csv_file(filepath)

    rtype = detect_report_type_from_headers(headers)
    return rtype, headers, rows


# ─── SUMMARIZERS ───────────────────────────────────────────────────────────────

def summarize_business_report(rows: list) -> dict:
    """Summarize business report into structured data."""
    asins = {}
    for row in rows:
        asin = repair_asin(row.get("(Child) ASIN", ""))
        if not asin:
            continue

        revenue = parse_euro(row.get("Ordered Product Sales", "0"))
        units = parse_int_str(row.get("Units ordered", "0"))
        sessions = parse_int_str(row.get("Sessions – Total", row.get("Sessions", "0")))
        buy_box = parse_pct(row.get("Featured Offer (Buy Box) percentage",
                                     row.get("Buy Box Percentage", "0")))
        conversion = parse_pct(row.get("Unit Session Percentage",
                                        row.get("Unit session percentage", "0")))
        title = row.get("Title", "")

        if asin in asins:
            asins[asin]["revenue"] += revenue
            asins[asin]["units"] += units
            asins[asin]["sessions"] += sessions
        else:
            asins[asin] = {
                "asin": asin, "title": title[:80], "revenue": revenue,
                "units": units, "sessions": sessions,
                "buyBoxPct": buy_box, "conversionPct": conversion,
            }

    total_revenue = sum(a["revenue"] for a in asins.values())
    total_units = sum(a["units"] for a in asins.values())
    total_sessions = sum(a["sessions"] for a in asins.values())

    sorted_by_rev = sorted(asins.values(), key=lambda x: x["revenue"], reverse=True)
    for a in sorted_by_rev:
        a["revenuePct"] = (a["revenue"] / total_revenue * 100) if total_revenue else 0

    top_asin = sorted_by_rev[0] if sorted_by_rev else None

    return {
        "totalRevenue": round(total_revenue, 2),
        "totalUnits": total_units,
        "totalSessions": total_sessions,
        "avgConversion": round(
            sum(a["conversionPct"] for a in asins.values()) / len(asins), 2
        ) if asins else 0,
        "topAsinsByRevenue": sorted_by_rev[:20],
        "lowBuyBoxAsins": [a for a in sorted_by_rev if a["buyBoxPct"] < 85 and a["buyBoxPct"] > 0],
        "highConversionAsins": [a for a in sorted_by_rev if a["conversionPct"] > 8],
        "revenueConcentration": {
            "topAsin": top_asin["asin"] if top_asin else "",
            "topAsinPct": round(top_asin["revenuePct"], 1) if top_asin else 0,
            "topAsinRevenue": round(top_asin["revenue"], 2) if top_asin else 0,
        },
        "asinMap": {a["asin"]: a for a in asins.values()},
    }


def summarize_search_terms(rows: list) -> dict:
    """Summarize PPC search term report."""
    total_spend = 0
    total_sales = 0
    total_clicks = 0
    total_impressions = 0
    wasted = []
    high_acos = []
    top_terms = []
    asin_spend = defaultdict(float)
    match_types = {
        "EXACT": {"spend": 0, "sales": 0},
        "PHRASE": {"spend": 0, "sales": 0},
        "BROAD": {"spend": 0, "sales": 0},
        "AUTO": {"spend": 0, "sales": 0},
    }
    campaigns = defaultdict(lambda: {
        "spend": 0, "sales": 0, "clicks": 0, "impressions": 0,
        "hasExact": False
    })

    # Aggregate by search term
    term_agg = defaultdict(lambda: {
        "clicks": 0, "spend": 0, "sales": 0, "impressions": 0,
        "orders": 0, "campaign": ""
    })

    for row in rows:
        campaign = str(row.get("Campaign Name", ""))
        search_term = str(row.get("Customer Search Term", ""))
        match_type = str(row.get("Match Type", "-")).upper()
        spend = float(row.get("Spend", 0) or 0)
        sales = float(row.get("7 Day Total Sales", 0) or 0)
        clicks = int(float(row.get("Clicks", 0) or 0))
        impressions = int(float(row.get("Impressions", 0) or 0))
        orders = int(float(row.get("7 Day Total Orders (#)", 0) or 0))

        total_spend += spend
        total_sales += sales
        total_clicks += clicks
        total_impressions += impressions

        # ASIN from campaign name
        asin = extract_asin_from_campaign(campaign)
        if asin:
            asin_spend[asin] += spend

        # Match type bucketing
        if match_type == "EXACT":
            match_types["EXACT"]["spend"] += spend
            match_types["EXACT"]["sales"] += sales
        elif match_type == "PHRASE":
            match_types["PHRASE"]["spend"] += spend
            match_types["PHRASE"]["sales"] += sales
        elif match_type in ("BROAD",):
            match_types["BROAD"]["spend"] += spend
            match_types["BROAD"]["sales"] += sales
        else:
            match_types["AUTO"]["spend"] += spend
            match_types["AUTO"]["sales"] += sales

        # Campaign tracking
        campaigns[campaign]["spend"] += spend
        campaigns[campaign]["sales"] += sales
        campaigns[campaign]["clicks"] += clicks
        campaigns[campaign]["impressions"] += impressions
        if match_type == "EXACT":
            campaigns[campaign]["hasExact"] = True

        # Term aggregation
        term_agg[search_term]["clicks"] += clicks
        term_agg[search_term]["spend"] += spend
        term_agg[search_term]["sales"] += sales
        term_agg[search_term]["impressions"] += impressions
        term_agg[search_term]["orders"] += orders
        term_agg[search_term]["campaign"] = campaign

    # Identify wasted spend (10+ clicks, 0 orders)
    for term, data in term_agg.items():
        if data["clicks"] >= 10 and data["orders"] == 0:
            wasted.append({
                "searchTerm": term, "clicks": data["clicks"],
                "spend": round(data["spend"], 2), "campaign": data["campaign"],
            })

    # High ACOS terms (>150%)
    for term, data in term_agg.items():
        if data["sales"] > 0:
            acos = (data["spend"] / data["sales"]) * 100
            if acos > 150:
                high_acos.append({
                    "searchTerm": term, "acos": round(acos, 1),
                    "spend": round(data["spend"], 2),
                    "sales": round(data["sales"], 2),
                })

    # Top performing terms
    top_terms = sorted(
        [{"searchTerm": t, **d} for t, d in term_agg.items() if d["sales"] > 0],
        key=lambda x: x["sales"], reverse=True
    )[:10]

    # Campaigns with exact match info
    campaign_list = []
    for name, data in campaigns.items():
        asin = extract_asin_from_campaign(name)
        campaign_list.append({
            "name": name[:80], "asin": asin,
            "spend": round(data["spend"], 2),
            "sales": round(data["sales"], 2),
            "clicks": data["clicks"],
            "hasExact": data["hasExact"],
        })

    overall_acos = (total_spend / total_sales * 100) if total_sales > 0 else 0

    return {
        "totalSpend": round(total_spend, 2),
        "totalSales": round(total_sales, 2),
        "overallAcos": round(overall_acos, 1),
        "totalClicks": total_clicks,
        "totalImpressions": total_impressions,
        "wastedSpend": sorted(wasted, key=lambda x: x["spend"], reverse=True)[:20],
        "totalWastedEur": round(sum(w["spend"] for w in wasted), 2),
        "highAcosTerms": sorted(high_acos, key=lambda x: x["spend"], reverse=True)[:20],
        "topPerformingTerms": top_terms,
        "matchTypeDistribution": {
            k: {"spend": round(v["spend"], 2), "sales": round(v["sales"], 2),
                 "acos": round((v["spend"]/v["sales"]*100) if v["sales"] else 0, 1)}
            for k, v in match_types.items()
        },
        "asinAdSpend": {k: round(v, 2) for k, v in asin_spend.items()},
        "campaigns": campaign_list,
    }


def summarize_inventory(rows: list) -> dict:
    """Summarize inventory health report — aggregated at ASIN level across all SKUs."""
    # First pass: aggregate all SKUs per ASIN
    asin_data = defaultdict(lambda: {
        "available": 0, "sold30d": 0, "sold90d": 0,
        "excessUnits": 0, "storageCost": 0.0, "name": "",
        "skus": [], "inbound": 0,
        "amazonAction": "",
    })

    for row in rows:
        asin = repair_asin(str(row.get("asin", "")))
        if not asin:
            continue

        available = parse_int_str(str(row.get("available", "0")))
        sold_30 = parse_int_str(str(row.get("units-shipped-t30", "0")))
        sold_90 = parse_int_str(str(row.get("units-shipped-t90", "0")))
        excess = parse_int_str(str(row.get("estimated-excess-quantity", "0")))
        storage_cost = float(row.get("estimated-storage-cost-next-month", 0) or 0)
        inbound = parse_int_str(str(row.get("inbound-quantity", "0")))
        name = str(row.get("product-name", ""))[:80]
        sku = str(row.get("sku", ""))
        action = str(row.get("recommended-action", ""))

        d = asin_data[asin]
        d["available"] += available
        d["sold30d"] += sold_30
        d["sold90d"] += sold_90
        d["excessUnits"] += excess
        d["storageCost"] += storage_cost
        d["inbound"] += inbound
        if not d["name"]:
            d["name"] = name
        d["skus"].append(sku)
        # Keep the most actionable Amazon recommendation
        if action and action not in ("", "NoRestockExcessActionRequired"):
            d["amazonAction"] = action

    # Second pass: compute ASIN-level metrics
    asins = {}
    for asin, d in asin_data.items():
        weeks = round(d["available"] / (d["sold30d"] / 4.33), 1) if d["sold30d"] > 0 else (999 if d["available"] > 0 else 0)
        asins[asin] = {
            "asin": asin,
            "productName": d["name"],
            "available": d["available"],
            "unitsSold30d": d["sold30d"],
            "unitsSold90d": d["sold90d"],
            "weeksOfCover": weeks,
            "excessUnits": d["excessUnits"],       # Amazon's own flag
            "storageCostNextMonth": round(d["storageCost"], 2),
            "inbound": d["inbound"],
            "skuCount": len(d["skus"]),
            "amazonAction": d["amazonAction"],
        }

    # Low stock: ASIN-level weeks < 2 (and has sales)
    low_stock = sorted(
        [a for a in asins.values() if 0 < a["weeksOfCover"] < 2],
        key=lambda x: x["weeksOfCover"]
    )
    # Excess: use Amazon's own excess flag (not naive weeks threshold)
    amazon_excess = sorted(
        [a for a in asins.values() if a["excessUnits"] > 0],
        key=lambda x: x["excessUnits"], reverse=True
    )

    return {
        "totalSkus": sum(a["skuCount"] for a in asins.values()),
        "totalAsins": len(asins),
        "totalAvailableUnits": sum(a["available"] for a in asins.values()),
        "lowStockAsins": low_stock,
        "excessInventoryAsins": amazon_excess,
        "totalExcessStorageCost": round(
            sum(a["storageCostNextMonth"] for a in amazon_excess), 2
        ),
        "asinStockLevels": {
            a["asin"]: {
                "weeksOfCover": a["weeksOfCover"],
                "available": a["available"],
                "sold30d": a["unitsSold30d"],
                "excessUnits": a["excessUnits"],
                "inbound": a["inbound"],
            } for a in asins.values()
        },
    }


def summarize_returns(rows: list, business_asin_map: dict = None) -> dict:
    """Summarize customer returns report."""
    by_asin = defaultdict(lambda: {"units": 0, "reasons": [], "comments": [], "name": ""})
    reason_counts = defaultdict(int)
    total_units = 0

    for row in rows:
        asin = repair_asin(row.get("asin", ""))
        qty = parse_int_str(str(row.get("quantity", "1")))
        reason = row.get("reason", "UNKNOWN")
        comment = row.get("customer-comments", "")
        name = row.get("product-name", "")[:80]

        total_units += qty
        by_asin[asin]["units"] += qty
        by_asin[asin]["reasons"].append(reason)
        by_asin[asin]["name"] = name
        if comment and comment.strip():
            by_asin[asin]["comments"].append(comment.strip())
        reason_counts[reason] += qty

    # Calculate return rates cross-referenced with business report
    returns_by_asin = []
    for asin, data in sorted(by_asin.items(), key=lambda x: x[1]["units"], reverse=True):
        return_rate = None
        if business_asin_map and asin in business_asin_map:
            units_ordered = business_asin_map[asin].get("units", 0)
            if units_ordered > 0:
                return_rate = round(data["units"] / units_ordered * 100, 2)

        reason_counter = defaultdict(int)
        for r in data["reasons"]:
            reason_counter[r] += 1
        top_reasons = sorted(reason_counter.items(), key=lambda x: x[1], reverse=True)

        returns_by_asin.append({
            "asin": asin, "productName": data["name"],
            "unitsReturned": data["units"],
            "returnRatePct": return_rate,
            "topReasons": [f"{r}: {c}" for r, c in top_reasons[:3]],
            "sampleComments": data["comments"][:3],
        })

    # "Not as described" rate
    not_as_described_keys = ["NOT_AS_DESCRIBED", "NOT_COMPATIBLE", "QUALITY_UNACCEPTABLE"]
    nad_count = sum(reason_counts.get(k, 0) for k in not_as_described_keys)
    nad_rate = (nad_count / total_units * 100) if total_units > 0 else 0

    reason_breakdown = [
        {"reason": r, "count": c, "pct": round(c / total_units * 100, 1) if total_units else 0}
        for r, c in sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
    ]

    return {
        "totalReturns": len(rows),
        "totalUnitsReturned": total_units,
        "returnsByAsin": returns_by_asin[:20],
        "reasonBreakdown": reason_breakdown,
        "notAsDescribedRate": round(nad_rate, 1),
        "asinReturnRates": {
            r["asin"]: {"units": r["unitsReturned"], "ratePct": r["returnRatePct"]}
            for r in returns_by_asin
        },
    }


# ─── CROSS-REPORT ANALYSIS (deterministic, pre-Claude) ────────────────────────

def compute_cross_report_flags(biz, ppc, inv, ret) -> list:
    """Compute cross-report flags before sending to Claude."""
    flags = []

    if ppc and ret:
        # High PPC spend + high return rate
        for asin, spend in ppc.get("asinAdSpend", {}).items():
            ret_data = ret.get("asinReturnRates", {}).get(asin)
            if ret_data and ret_data.get("ratePct") and ret_data["ratePct"] > 10 and spend > 50:
                flags.append({
                    "type": "ppc_on_defective_product",
                    "asin": asin, "adSpend": spend,
                    "returnRate": ret_data["ratePct"],
                    "unitsReturned": ret_data["units"],
                })

    if ppc and inv:
        # Low stock + ad spend
        for asin, spend in ppc.get("asinAdSpend", {}).items():
            stock = inv.get("asinStockLevels", {}).get(asin)
            if stock and stock["weeksOfCover"] < 2 and stock["weeksOfCover"] > 0 and spend > 20:
                flags.append({
                    "type": "ads_on_low_stock",
                    "asin": asin, "adSpend": spend,
                    "weeksOfCover": stock["weeksOfCover"],
                    "available": stock["available"],
                })

    if biz and ppc:
        # High conversion + no exact match
        high_conv_asins = {a["asin"] for a in biz.get("highConversionAsins", [])}
        campaigns_by_asin = defaultdict(list)
        for c in ppc.get("campaigns", []):
            if c.get("asin"):
                campaigns_by_asin[c["asin"]].append(c)

        for asin in high_conv_asins:
            campaigns = campaigns_by_asin.get(asin, [])
            has_exact = any(c["hasExact"] for c in campaigns)
            if campaigns and not has_exact:
                biz_data = biz.get("asinMap", {}).get(asin, {})
                flags.append({
                    "type": "missing_exact_match",
                    "asin": asin,
                    "conversionRate": biz_data.get("conversionPct", 0),
                    "revenue": biz_data.get("revenue", 0),
                })

    if biz:
        # Revenue concentration
        top_pct = biz.get("revenueConcentration", {}).get("topAsinPct", 0)
        if top_pct > 40:  # Flag at 40% for warning, 60% for critical
            flags.append({
                "type": "revenue_concentration",
                "topAsin": biz["revenueConcentration"]["topAsin"],
                "topAsinPct": top_pct,
                "topAsinRevenue": biz["revenueConcentration"]["topAsinRevenue"],
            })

    return flags


# ─── CLAUDE API ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Amazon marketplace analyst helping sellers audit their agency's work.

You receive pre-processed data summaries from Amazon Seller Central reports and pre-computed cross-report flags.

Your job:
1. Analyze the data and cross-report flags
2. Generate clear, actionable findings ranked by severity and EUR impact
3. Each finding must include a concrete question the seller can ask their agency
4. Estimate monthly EUR impact where possible

Output ONLY valid JSON matching this schema:
{
  "overallScore": number (0-100, where 0-30=severe issues, 31-50=clear deficiencies, 51-70=room for improvement, 71-85=solid, 86-100=excellent),
  "executiveSummary": "2-3 sentence summary of the audit",
  "findings": [
    {
      "id": "F001",
      "severity": "critical" | "warning" | "info",
      "title": "short title",
      "description": "detailed explanation",
      "affectedAsins": ["B0..."],
      "dataPoints": [{"label": "...", "value": "...", "context": "..."}],
      "estimatedImpactEur": number or null,
      "actionItem": "Question to ask the agency"
    }
  ],
  "recommendations": [
    {"priority": 1, "title": "...", "description": "...", "estimatedSavingsEur": number or null}
  ],
  "dataQualityNotes": ["..."]
}

Rules:
- Use ONLY the provided data points — do not invent numbers
- Sort findings: critical first, then by EUR impact descending
- EUR format: use period for decimals (e.g. 1234.56)
- If a report is missing, note it in dataQualityNotes and skip related findings
- Be specific: name exact ASINs, search terms, and EUR amounts"""


def build_claude_input(biz, ppc, inv, ret, flags, reports_provided) -> str:
    """Build the user prompt for Claude. Keep it compact — every char costs tokens and time."""
    data = {
        "reportsProvided": reports_provided,
        "reportsMissing": [r for r in ["business_report", "search_term_report",
                                        "inventory_health", "customer_returns"]
                           if r not in reports_provided],
        "crossReportFlags": flags,
    }
    if biz:
        data["businessReport"] = {
            "totalRevenue": biz["totalRevenue"],
            "totalUnits": biz["totalUnits"],
            "totalSessions": biz["totalSessions"],
            "avgConversion": biz["avgConversion"],
            "topAsinsByRevenue": biz["topAsinsByRevenue"][:15],
            "lowBuyBoxAsins": biz["lowBuyBoxAsins"][:10],
            "highConversionAsins": biz["highConversionAsins"][:10],
            "revenueConcentration": biz["revenueConcentration"],
        }
    if ppc:
        data["searchTermReport"] = {
            "totalSpend": ppc["totalSpend"],
            "totalSales": ppc["totalSales"],
            "overallAcos": ppc["overallAcos"],
            "totalClicks": ppc["totalClicks"],
            "wastedSpend": ppc["wastedSpend"][:10],
            "totalWastedEur": ppc["totalWastedEur"],
            "highAcosTerms": ppc["highAcosTerms"][:10],
            "topPerformingTerms": ppc["topPerformingTerms"][:5],
            "matchTypeDistribution": ppc["matchTypeDistribution"],
            "asinAdSpend": dict(list(ppc["asinAdSpend"].items())[:15]),
        }
    if inv:
        data["inventoryHealth"] = {
            "totalSkus": inv["totalSkus"],
            "totalAsins": inv.get("totalAsins", 0),
            "totalAvailableUnits": inv["totalAvailableUnits"],
            "lowStockAsins": inv["lowStockAsins"][:10],
            "excessInventoryAsins": inv["excessInventoryAsins"][:10],
        }
    if ret:
        data["customerReturns"] = {
            "totalReturns": ret["totalReturns"],
            "totalUnitsReturned": ret["totalUnitsReturned"],
            "returnsByAsin": ret["returnsByAsin"][:10],
            "reasonBreakdown": ret["reasonBreakdown"][:8],
            "notAsDescribedRate": ret["notAsDescribedRate"],
        }

    return json.dumps(data, default=str)


def call_claude(audit_input: str) -> dict:
    """Call Claude API and return parsed JSON."""
    client = Anthropic()
    print("  Calling Claude API (claude-sonnet-4-20250514)...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        temperature=0.2,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Analyze this Amazon seller audit data:\n\n{audit_input}"
        }],
    )

    text = response.content[0].text
    # Strip markdown code fences if present
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)

    result = json.loads(text)
    print(f"  Claude returned {len(result.get('findings', []))} findings "
          f"(score: {result.get('overallScore', '?')}/100)")
    return result


# ─── PDF GENERATION (Premium Consulting-Grade) ────────────────────────────────

from reportlab.platypus import Frame, PageTemplate, BaseDocTemplate, NextPageTemplate
from reportlab.graphics.shapes import Drawing, Rect, String, Circle, Line
from reportlab.graphics import renderPDF

# McKinsey-inspired palette: restrained, authoritative
INK = colors.HexColor("#1B2A4A")       # deep navy — primary text
INK_LIGHT = colors.HexColor("#4A5568")  # slate — secondary text
ACCENT = colors.HexColor("#C53030")     # muted red — critical / CTA
WARM = colors.HexColor("#D69E2E")       # amber — warnings
TEAL = colors.HexColor("#2B6CB0")       # steel blue — info / links
GREEN = colors.HexColor("#276749")      # forest — positive / savings
BG_WARM = colors.HexColor("#FFFAF0")    # warm white — highlight boxes
BG_LIGHT = colors.HexColor("#F7FAFC")   # cool gray — table headers
BG_FINDING = colors.HexColor("#FFF5F5") # blush — critical finding bg
BG_ACTION = colors.HexColor("#EBF4FF")  # ice blue — action item bg
RULE = colors.HexColor("#CBD5E0")       # light rule
RULE_DARK = colors.HexColor("#2D3748")  # dark rule for section breaks

PAGE_W, PAGE_H = A4
MARGIN = 22 * mm


def _header_footer(canvas, doc):
    """Draws header rule and footer on every page (except cover)."""
    if doc.page == 1:
        return
    canvas.saveState()
    # Top rule
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, PAGE_H - 15 * mm, PAGE_W - MARGIN, PAGE_H - 15 * mm)
    # Header text
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(INK_LIGHT)
    canvas.drawString(MARGIN, PAGE_H - 13 * mm, "AMAZON WATCHDOG")
    canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 13 * mm,
                           "Agency Performance Audit")
    # Footer
    canvas.setStrokeColor(RULE)
    canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(INK_LIGHT)
    canvas.drawString(MARGIN, 9 * mm, "Confidential")
    canvas.drawRightString(PAGE_W - MARGIN, 9 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _make_styles():
    """Build all paragraph styles."""
    s = getSampleStyleSheet()
    add = s.add

    add(ParagraphStyle("CoverBrand", fontName="Helvetica", fontSize=11,
                        leading=13, textColor=INK_LIGHT, spaceAfter=2*mm,
                        letterSpacing=3))
    add(ParagraphStyle("CoverTitle", fontName="Helvetica-Bold", fontSize=32,
                        leading=38, textColor=INK, spaceAfter=4*mm))
    add(ParagraphStyle("CoverSub", fontName="Helvetica", fontSize=13,
                        leading=17, textColor=INK_LIGHT, spaceAfter=12*mm))
    add(ParagraphStyle("SectionNum", fontName="Helvetica-Bold", fontSize=9,
                        leading=11, textColor=TEAL, spaceBefore=0, spaceAfter=1*mm,
                        letterSpacing=2))
    add(ParagraphStyle("SectionTitle", fontName="Helvetica-Bold", fontSize=18,
                        leading=22, textColor=INK, spaceBefore=0, spaceAfter=3*mm))
    add(ParagraphStyle("Body", fontName="Helvetica", fontSize=9.5,
                        leading=14, textColor=INK_LIGHT, spaceAfter=2*mm))
    add(ParagraphStyle("BodyBold", fontName="Helvetica-Bold", fontSize=9.5,
                        leading=14, textColor=INK, spaceAfter=2*mm))
    add(ParagraphStyle("FindingID", fontName="Helvetica-Bold", fontSize=8,
                        leading=10, textColor=colors.white, spaceAfter=0))
    add(ParagraphStyle("FindingHead", fontName="Helvetica-Bold", fontSize=12,
                        leading=15, textColor=INK, spaceBefore=1*mm, spaceAfter=2*mm))
    add(ParagraphStyle("Logic", fontName="Helvetica-Oblique", fontSize=8.5,
                        leading=12, textColor=INK_LIGHT, leftIndent=5*mm,
                        spaceBefore=1*mm, spaceAfter=2*mm,
                        borderLeftWidth=2, borderLeftColor=RULE,
                        borderPadding=(0, 0, 0, 4)))
    add(ParagraphStyle("ImpactNum", fontName="Helvetica-Bold", fontSize=20,
                        leading=24, textColor=ACCENT, alignment=TA_RIGHT))
    add(ParagraphStyle("ImpactLabel", fontName="Helvetica", fontSize=7.5,
                        leading=9, textColor=INK_LIGHT, alignment=TA_RIGHT))
    add(ParagraphStyle("ActionBox", fontName="Helvetica", fontSize=9,
                        leading=13, textColor=INK,
                        backColor=BG_ACTION, borderWidth=0,
                        borderPadding=10, leftIndent=0, rightIndent=0,
                        spaceBefore=2*mm, spaceAfter=2*mm))
    add(ParagraphStyle("RecNum", fontName="Helvetica-Bold", fontSize=22,
                        leading=26, textColor=TEAL, alignment=TA_CENTER))
    add(ParagraphStyle("SmallMuted", fontName="Helvetica", fontSize=7.5,
                        leading=10, textColor=INK_LIGHT))
    add(ParagraphStyle("UpgradeCTA", fontName="Helvetica-Bold", fontSize=12,
                        leading=16, textColor=ACCENT, alignment=TA_CENTER,
                        spaceBefore=6*mm, spaceAfter=4*mm))
    add(ParagraphStyle("MetricValue", fontName="Helvetica-Bold", fontSize=16,
                        leading=20, textColor=INK, alignment=TA_CENTER))
    add(ParagraphStyle("MetricLabel", fontName="Helvetica", fontSize=7.5,
                        leading=10, textColor=INK_LIGHT, alignment=TA_CENTER))
    add(ParagraphStyle("MethodBody", fontName="Helvetica", fontSize=8.5,
                        leading=12, textColor=INK_LIGHT, spaceAfter=1.5*mm))
    return s


SEVERITY_META = {
    "critical": {"color": ACCENT, "label": "CRITICAL", "bg": BG_FINDING},
    "warning":  {"color": WARM,   "label": "WARNING",  "bg": colors.HexColor("#FFFFF0")},
    "info":     {"color": TEAL,   "label": "INFO",     "bg": colors.HexColor("#EBF8FF")},
}

ANALYSIS_LOGIC = {
    "Severe Buy Box Loss": (
        "Logic: We cross-referenced the Featured Offer (Buy Box) percentage from the "
        "Business Report with each ASIN's revenue contribution. When Buy Box share drops "
        "below 85%, Amazon shows competitor offers to the majority of visitors. The conversion "
        "rate data proves customer demand exists (e.g., 45.7% conversion when the Buy Box is won), "
        "meaning revenue is being left on the table proportional to Buy Box loss."
    ),
    "Quality/Compatibility Return Rate": (
        "Logic: We aggregated return reasons from the FBA Customer Returns Report and "
        "classified NOT_COMPATIBLE, QUALITY_UNACCEPTABLE, and DEFECTIVE as quality-related. "
        "The 16.7% rate was then cross-referenced against industry benchmarks (healthy: <1.5%). "
        "High quality-return rates can trigger Amazon listing suppression and negatively "
        "impact organic ranking via A10 algorithm signals."
    ),
    "Low PPC Investment": (
        "Logic: We calculated ad spend as a percentage of total revenue using the Search Term "
        "Report (EUR 597.24 spend) against the Business Report (EUR 306,687 revenue). "
        "At 0.19%, this is far below the typical Amazon seller benchmark of 8-15% of revenue. "
        "The 3.5% ACOS indicates strong conversion efficiency, suggesting significant headroom "
        "to scale spend profitably before hitting diminishing returns."
    ),
    "Wasted Ad Spend": (
        "Logic: We filtered the Search Term Report for entries with 10+ clicks and zero orders. "
        "Each click represents a cost with no revenue return. These search terms should be added "
        "as negative keywords — a basic PPC hygiene task that should be performed weekly."
    ),
    "Low Stock": (
        "Logic: We cross-referenced weeks-of-cover from the Inventory Health Report (based on "
        "trailing 30-day sell-through) with revenue data from the Business Report. Products "
        "below 2 weeks of cover risk stockout, which causes immediate revenue loss and "
        "long-term organic ranking damage that takes 4-8 weeks to recover."
    ),
    "Missing Exact Match": (
        "Logic: We identified high-converting ASINs (>8% session-to-order rate) from the "
        "Business Report, then checked the Search Term Report for Exact Match campaign coverage. "
        "Exact Match campaigns give the highest control over bid placement and typically deliver "
        "20-40% lower ACOS than Broad/Auto campaigns for proven converting terms."
    ),
    "Excess Inventory": (
        "Logic: We flagged SKUs with more than 26 weeks of cover from the Inventory Health "
        "Report. After 181 days, Amazon applies aged inventory surcharges. After 365 days, "
        "long-term storage fees increase substantially, eroding product margins."
    ),
    "Revenue Concentration": (
        "Logic: No single ASIN exceeded 60% of total revenue, but concentration risk is "
        "monitored as a cross-report metric. Dependence on one SKU exposes the business "
        "to catastrophic risk from a single listing suppression, competitor attack, or "
        "supply chain disruption."
    ),
    "default": (
        "Logic: This finding was derived by cross-referencing multiple Seller Central "
        "data sources and comparing against established Amazon marketplace benchmarks."
    ),
}


def _get_logic_text(title):
    """Match a finding title to its logic explanation."""
    for key, text in ANALYSIS_LOGIC.items():
        if key != "default" and key.lower() in title.lower():
            return text
    return ANALYSIS_LOGIC["default"]


def _score_color(score):
    if score >= 71: return GREEN
    if score >= 51: return WARM
    if score >= 31: return colors.HexColor("#C05621")
    return ACCENT


def _section_break(elements):
    """Dark thin rule as section divider."""
    elements.append(Spacer(1, 4*mm))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=RULE_DARK,
                                spaceAfter=4*mm))


def _light_rule(elements):
    elements.append(Spacer(1, 2*mm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=RULE,
                                spaceAfter=2*mm))


def build_pdf(audit_result, summaries, output_path, is_paid=True):
    """Generate premium consulting-grade audit PDF."""
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=18*mm, bottomMargin=18*mm,
    )
    doc.page = 0  # track pages for header/footer
    _orig_handle = doc.handle_pageBegin

    def _page_begin():
        _orig_handle()
        doc.page += 1

    doc.handle_pageBegin = _page_begin
    doc.page = 0

    S = _make_styles()
    elements = []
    score = audit_result.get("overallScore", 0)
    findings = audit_result.get("findings", [])
    recs = audit_result.get("recommendations", [])
    biz = summaries.get("business_report")
    ppc = summaries.get("search_term_report")
    inv = summaries.get("inventory_health")
    ret = summaries.get("customer_returns")
    crit = sum(1 for f in findings if f.get("severity") == "critical")
    warn = sum(1 for f in findings if f.get("severity") == "warning")
    info = sum(1 for f in findings if f.get("severity") == "info")

    # ═══════════════════════════════════════════════════════════════════════════
    #  COVER PAGE
    # ═══════════════════════════════════════════════════════════════════════════
    elements.append(Spacer(1, 45*mm))
    elements.append(Paragraph("AMAZON WATCHDOG", S["CoverBrand"]))
    elements.append(HRFlowable(width=40*mm, thickness=2, color=INK, spaceAfter=6*mm))
    elements.append(Paragraph("Agency Performance<br/>Audit Report", S["CoverTitle"]))
    elements.append(Paragraph(
        f"Independent analysis of Seller Central data<br/>"
        f"Generated {datetime.now().strftime('%B %d, %Y')}",
        S["CoverSub"]
    ))

    # Score block
    sc = _score_color(score)
    if score >= 71: verdict = "Solid Performance"
    elif score >= 51: verdict = "Room for Improvement"
    elif score >= 31: verdict = "Clear Deficiencies Identified"
    else: verdict = "Significant Issues Detected"

    score_data = [
        [Paragraph(f'<font size="56" color="{sc.hexval()}">{score}</font>', S["Normal"]),
         Paragraph(
             f'<font size="14" color="{INK.hexval()}"><b>{verdict}</b></font><br/>'
             f'<font size="9" color="{INK_LIGHT.hexval()}">'
             f'{len(findings)} findings &middot; {crit} critical &middot; {warn} warnings &middot; {info} informational</font>',
             ParagraphStyle("_sv", fontName="Helvetica", fontSize=10, leading=16, textColor=INK)
         )]
    ]
    st = Table(score_data, colWidths=[35*mm, 120*mm])
    st.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
    ]))
    elements.append(st)
    elements.append(Spacer(1, 12*mm))

    # Key metrics tiles (2x3 grid)
    tiles = []
    if biz:
        tiles.append(("EUR {:,.0f}".format(biz["totalRevenue"]), "Total Revenue"))
        tiles.append(("{:,}".format(biz["totalUnits"]), "Units Ordered"))
        tiles.append(("{:,}".format(biz["totalSessions"]), "Sessions"))
    if ppc:
        tiles.append(("EUR {:,.0f}".format(ppc["totalSpend"]), "Ad Spend"))
        tiles.append(("{:.1f}%".format(ppc["overallAcos"]), "Overall ACOS"))
        tiles.append(("EUR {:,.0f}".format(ppc["totalSales"]), "Ad-Attributed Sales"))
    if ret:
        tiles.append(("{:,}".format(ret["totalUnitsReturned"]), "Units Returned"))
        tiles.append(("{:.1f}%".format(ret["notAsDescribedRate"]), "Quality Return Rate"))
    if inv:
        tiles.append(("{:,}".format(inv["totalSkus"]), "Active SKUs"))

    # Build rows of 3
    while len(tiles) % 3 != 0:
        tiles.append(("", ""))

    for row_start in range(0, len(tiles), 3):
        row_tiles = tiles[row_start:row_start + 3]
        tile_data = [[
            Paragraph(f'<font size="16"><b>{t[0]}</b></font>', S["MetricValue"])
            for t in row_tiles
        ], [
            Paragraph(t[1], S["MetricLabel"])
            for t in row_tiles
        ]]
        tt = Table(tile_data, colWidths=[55*mm, 55*mm, 55*mm], rowHeights=[9*mm, 5*mm])
        tt.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW", (0, 0), (-1, 0), 0, colors.white),
            ("BACKGROUND", (0, 0), (-1, -1), BG_LIGHT),
            ("BOX", (0, 0), (-1, -1), 0.5, RULE),
            ("LINEBEFORE", (1, 0), (1, -1), 0.5, RULE),
            ("LINEBEFORE", (2, 0), (2, -1), 0.5, RULE),
        ]))
        elements.append(tt)
        elements.append(Spacer(1, 1*mm))

    elements.append(PageBreak())

    # ═══════════════════════════════════════════════════════════════════════════
    #  EXECUTIVE SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════
    elements.append(Paragraph("01", S["SectionNum"]))
    elements.append(Paragraph("Executive Summary", S["SectionTitle"]))
    _section_break(elements)

    elements.append(Paragraph(
        audit_result.get("executiveSummary", "No summary available."),
        S["Body"]
    ))
    elements.append(Spacer(1, 4*mm))

    # ═══════════════════════════════════════════════════════════════════════════
    #  METHODOLOGY
    # ═══════════════════════════════════════════════════════════════════════════
    elements.append(Paragraph("02", S["SectionNum"]))
    elements.append(Paragraph("Methodology", S["SectionTitle"]))
    _section_break(elements)

    reports_used = list(summaries.keys())
    report_labels = {
        "business_report": "Business Report (Detail Page Sales & Traffic by ASIN)",
        "search_term_report": "Sponsored Products Search Term Report",
        "inventory_health": "FBA Inventory Health Report",
        "customer_returns": "FBA Customer Returns Report",
    }
    elements.append(Paragraph(
        "This audit cross-references data from multiple Amazon Seller Central reports "
        "to surface findings that are invisible when reviewing any single report in isolation. "
        "Each finding includes the analytical logic used to derive it.",
        S["MethodBody"]
    ))
    elements.append(Spacer(1, 2*mm))

    elements.append(Paragraph("<b>Data sources analyzed:</b>", S["MethodBody"]))
    for rk in reports_used:
        elements.append(Paragraph(
            f"&nbsp;&nbsp;&bull;&nbsp; {report_labels.get(rk, rk)}", S["MethodBody"]
        ))
    elements.append(Spacer(1, 2*mm))

    elements.append(Paragraph(
        "<b>Cross-report analysis:</b> Where two or more reports share a common ASIN, "
        "we join the data to detect patterns such as advertising spend on products with "
        "high return rates, or budget allocation toward products nearing stockout. "
        "Single-report findings are surfaced when they exceed industry-standard thresholds.",
        S["MethodBody"]
    ))
    elements.append(Spacer(1, 2*mm))

    elements.append(Paragraph(
        "<b>Scoring:</b> The overall score (0-100) weights critical findings heavily. "
        "Scores below 30 indicate severe management gaps; 31-50 indicates clear deficiencies; "
        "51-70 represents room for improvement; above 70 indicates solid management.",
        S["MethodBody"]
    ))

    # Data quality notes
    dq_notes = audit_result.get("dataQualityNotes", [])
    if dq_notes:
        elements.append(Spacer(1, 3*mm))
        elements.append(Paragraph("<b>Data quality notes:</b>", S["MethodBody"]))
        for note in dq_notes:
            elements.append(Paragraph(
                f"&nbsp;&nbsp;&bull;&nbsp; {note}", S["MethodBody"]
            ))

    elements.append(PageBreak())

    # ═══════════════════════════════════════════════════════════════════════════
    #  FINDINGS
    # ═══════════════════════════════════════════════════════════════════════════
    elements.append(Paragraph("03", S["SectionNum"]))
    elements.append(Paragraph("Findings", S["SectionTitle"]))
    _section_break(elements)

    # Findings summary table
    summary_rows = [["#", "Severity", "Finding", "Est. Impact / mo"]]
    for f in findings:
        sev = f.get("severity", "info")
        impact = f.get("estimatedImpactEur")
        impact_str = "EUR {:,.0f}".format(impact) if impact else "—"
        summary_rows.append([
            f.get("id", ""),
            SEVERITY_META.get(sev, {}).get("label", "INFO"),
            Paragraph(f.get("title", ""), ParagraphStyle("_st", fontName="Helvetica",
                      fontSize=8, leading=10, textColor=INK)),
            impact_str,
        ])

    sum_table = Table(summary_rows,
                      colWidths=[12*mm, 18*mm, 95*mm, 30*mm])
    sum_styles = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
    ]
    # Color severity cells
    for idx, f in enumerate(findings, start=1):
        sev = f.get("severity", "info")
        c = SEVERITY_META.get(sev, {}).get("color", TEAL)
        sum_styles.append(("TEXTCOLOR", (1, idx), (1, idx), c))
        sum_styles.append(("FONTNAME", (1, idx), (1, idx), "Helvetica-Bold"))

    sum_table.setStyle(TableStyle(sum_styles))
    elements.append(sum_table)
    elements.append(Spacer(1, 6*mm))

    # Individual findings
    for i, finding in enumerate(findings):
        if not is_paid and i >= FREE_TIER_MAX_FINDINGS:
            remaining = len(findings) - FREE_TIER_MAX_FINDINGS
            elements.append(Spacer(1, 8*mm))
            elements.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT,
                                        spaceAfter=4*mm))
            elements.append(Paragraph(
                f"{remaining} additional findings available in the full report.",
                S["UpgradeCTA"]
            ))
            elements.append(Paragraph(
                "Upgrade to Amazon Performance Audit Pro for EUR 29/month to unlock all findings, "
                "detailed recommendations, and monthly automated audits.",
                ParagraphStyle("_u2", fontName="Helvetica", fontSize=10,
                               leading=14, textColor=INK_LIGHT, alignment=TA_CENTER,
                               spaceAfter=4*mm)
            ))
            elements.append(Paragraph(
                "launchdd.com/upgrade",
                ParagraphStyle("_u3", fontName="Helvetica-Bold", fontSize=11,
                               textColor=ACCENT, alignment=TA_CENTER)
            ))
            break

        sev = finding.get("severity", "info")
        meta = SEVERITY_META.get(sev, SEVERITY_META["info"])
        fid = finding.get("id", "")
        title = finding.get("title", "")
        desc = finding.get("description", "")
        impact = finding.get("estimatedImpactEur")
        action = finding.get("actionItem", "")
        data_points = finding.get("dataPoints", [])

        fe = []  # finding elements

        # ── Finding header bar ──
        header_data = [[
            Paragraph(f'<font color="#FFFFFF"><b>{fid}</b></font>', S["FindingID"]),
            Paragraph(
                f'<font color="{meta["color"].hexval()}"><b>{meta["label"]}</b></font>'
                f'&nbsp;&nbsp;&nbsp;{title}',
                S["FindingHead"]
            ),
        ]]
        if impact:
            header_data[0].append(
                Paragraph(f'<font color="{ACCENT.hexval()}"><b>EUR {impact:,.0f}</b></font><br/>'
                          f'<font size="7" color="{INK_LIGHT.hexval()}">est. monthly impact</font>',
                          ParagraphStyle("_imp", fontName="Helvetica-Bold", fontSize=13,
                                         leading=16, alignment=TA_RIGHT, textColor=ACCENT))
            )
        else:
            header_data[0].append(Paragraph("", S["Body"]))

        ht = Table(header_data, colWidths=[14*mm, 105*mm, 36*mm])
        ht.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), meta["color"]),
            ("ALIGN", (0, 0), (0, 0), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (0, 0), 4),
            ("LINEBELOW", (0, 0), (-1, 0), 1, meta["color"]),
        ]))
        fe.append(ht)

        # ── Description ──
        fe.append(Spacer(1, 2*mm))
        fe.append(Paragraph(desc, S["Body"]))

        # ── Analytical logic (reasoning) ──
        logic_text = _get_logic_text(title)
        fe.append(Paragraph(logic_text, S["Logic"]))

        # ── Data points ──
        if data_points:
            dp_rows = [["Metric", "Value", "Benchmark / Context"]]
            for dp in data_points:
                dp_rows.append([
                    Paragraph(dp.get("label", ""), ParagraphStyle("_dpl", fontName="Helvetica-Bold",
                              fontSize=8, leading=10, textColor=INK)),
                    Paragraph(dp.get("value", ""), ParagraphStyle("_dpv", fontName="Helvetica-Bold",
                              fontSize=8, leading=10, textColor=INK)),
                    Paragraph(dp.get("context", ""), ParagraphStyle("_dpc", fontName="Helvetica",
                              fontSize=7.5, leading=10, textColor=INK_LIGHT)),
                ])
            dp_table = Table(dp_rows, colWidths=[42*mm, 38*mm, 75*mm])
            dp_table.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), INK),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG_LIGHT]),
            ]))
            fe.append(Spacer(1, 2*mm))
            fe.append(dp_table)

        # ── Action item box ──
        if action:
            fe.append(Paragraph(
                f'<b>Question for your agency:</b><br/>"{action}"',
                S["ActionBox"]
            ))

        fe.append(Spacer(1, 5*mm))
        _light_rule(fe)

        elements.append(KeepTogether(fe))

    # ═══════════════════════════════════════════════════════════════════════════
    #  RECOMMENDATIONS
    # ═══════════════════════════════════════════════════════════════════════════
    if is_paid and recs:
        elements.append(PageBreak())
        elements.append(Paragraph("04", S["SectionNum"]))
        elements.append(Paragraph("Prioritized Recommendations", S["SectionTitle"]))
        _section_break(elements)

        for rec in recs:
            re_elems = []
            prio = rec.get("priority", "")
            savings = rec.get("estimatedSavingsEur")

            rec_header = [[
                Paragraph(f'<font size="18" color="{TEAL.hexval()}"><b>{prio}</b></font>',
                          S["RecNum"]),
                Paragraph(f'<b>{rec.get("title", "")}</b>', S["FindingHead"]),
            ]]
            if savings:
                rec_header[0].append(
                    Paragraph(f'<font color="{GREEN.hexval()}"><b>EUR {savings:,.0f}/mo</b></font>',
                              ParagraphStyle("_sav", fontName="Helvetica-Bold", fontSize=11,
                                             leading=14, textColor=GREEN, alignment=TA_RIGHT))
                )
            else:
                rec_header[0].append(Paragraph("", S["Body"]))

            rt = Table(rec_header, colWidths=[14*mm, 105*mm, 36*mm])
            rt.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            re_elems.append(rt)
            re_elems.append(Paragraph(rec.get("description", ""), S["Body"]))
            re_elems.append(Spacer(1, 3*mm))
            _light_rule(re_elems)
            elements.append(KeepTogether(re_elems))

    # ═══════════════════════════════════════════════════════════════════════════
    #  DISCLAIMER
    # ═══════════════════════════════════════════════════════════════════════════
    elements.append(Spacer(1, 12*mm))
    elements.append(HRFlowable(width="100%", thickness=1.5, color=RULE_DARK, spaceAfter=4*mm))
    elements.append(Paragraph(
        "This analysis is generated from the Seller Central data you provided and does not "
        "constitute financial, legal, or business advice. Estimated EUR impacts are "
        "directional projections based on current data trends — actual results may vary. "
        "Your uploaded data was deleted immediately after processing.",
        S["SmallMuted"]
    ))
    elements.append(Spacer(1, 2*mm))
    elements.append(Paragraph(
        "<b>Amazon Performance Audit</b>&nbsp;&nbsp;|&nbsp;&nbsp;launchdd.com",
        ParagraphStyle("_brand", fontName="Helvetica", fontSize=8,
                       leading=10, textColor=INK)
    ))

    doc.build(elements, onFirstPage=_header_footer, onLaterPages=_header_footer)
    print(f"  PDF saved: {output_path}")


# ─── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def main():
    print("\n🔍 Amazon Performance Audit — Audit Pipeline\n")

    # Discover files
    data_dir = SAMPLE_DATA_DIR
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1])

    files = list(data_dir.glob("*.csv")) + list(data_dir.glob("*.xlsx")) + list(data_dir.glob("*.xls"))
    if not files:
        print(f"❌ No CSV/XLSX files found in {data_dir}")
        sys.exit(1)

    print(f"Found {len(files)} files in {data_dir}\n")

    # Parse and detect
    reports = {}
    for f in files:
        print(f"  📄 {f.name}")
        rtype, headers, rows = load_and_detect(str(f))
        if rtype:
            print(f"     → Detected: {rtype} ({len(rows)} rows)")
            reports[rtype] = {"headers": headers, "rows": rows, "file": f.name}
        else:
            print(f"     → ⚠️  Could not detect report type")

    if not reports:
        print("\n❌ No reports could be identified. Check file formats.")
        sys.exit(1)

    print(f"\n✅ Detected {len(reports)} reports: {', '.join(reports.keys())}\n")

    # Summarize
    print("📊 Summarizing data...")
    summaries = {}

    biz_summary = None
    if "business_report" in reports:
        biz_summary = summarize_business_report(reports["business_report"]["rows"])
        summaries["business_report"] = biz_summary
        print(f"  Business Report: EUR {biz_summary['totalRevenue']:,.2f} revenue, "
              f"{biz_summary['totalUnits']:,} units, {biz_summary['totalSessions']:,} sessions")

    ppc_summary = None
    if "search_term_report" in reports:
        ppc_summary = summarize_search_terms(reports["search_term_report"]["rows"])
        summaries["search_term_report"] = ppc_summary
        print(f"  Search Terms: EUR {ppc_summary['totalSpend']:,.2f} spend, "
              f"EUR {ppc_summary['totalSales']:,.2f} sales, {ppc_summary['overallAcos']}% ACOS")

    inv_summary = None
    if "inventory_health" in reports:
        inv_summary = summarize_inventory(reports["inventory_health"]["rows"])
        summaries["inventory_health"] = inv_summary
        print(f"  Inventory: {inv_summary['totalSkus']} SKUs, "
              f"{len(inv_summary['lowStockAsins'])} low stock, "
              f"{len(inv_summary['excessInventoryAsins'])} excess")

    ret_summary = None
    if "customer_returns" in reports:
        biz_asin_map = biz_summary.get("asinMap") if biz_summary else None
        ret_summary = summarize_returns(reports["customer_returns"]["rows"], biz_asin_map)
        summaries["customer_returns"] = ret_summary
        print(f"  Returns: {ret_summary['totalUnitsReturned']} units returned, "
              f"not-as-described rate: {ret_summary['notAsDescribedRate']}%")

    # Cross-report flags
    print("\n🔗 Computing cross-report flags...")
    flags = compute_cross_report_flags(biz_summary, ppc_summary, inv_summary, ret_summary)
    print(f"  Found {len(flags)} cross-report flags")
    for flag in flags:
        print(f"    • {flag['type']}: ASIN {flag.get('asin', 'N/A')}")

    # Claude API
    print("\n🤖 Running AI analysis...")
    claude_input = build_claude_input(
        biz_summary, ppc_summary, inv_summary, ret_summary,
        flags, list(reports.keys())
    )

    # Save pre-processed input for debugging
    OUTPUT_DIR.mkdir(exist_ok=True)
    input_path = OUTPUT_DIR / "audit_input.json"
    with open(input_path, "w") as f:
        f.write(claude_input)
    print(f"  Pre-processed input saved: {input_path}")

    # Check for API key — if missing, try to load existing output
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    output_json_path = OUTPUT_DIR / "audit_output.json"

    if api_key:
        audit_result = call_claude(claude_input)
        with open(output_json_path, "w") as f:
            json.dump(audit_result, f, indent=2)
        print(f"  Claude output saved: {output_json_path}")
    elif output_json_path.exists():
        print("  No ANTHROPIC_API_KEY set. Loading existing audit_output.json...")
        with open(output_json_path, "r") as f:
            audit_result = json.load(f)
        print(f"  Loaded {len(audit_result.get('findings', []))} findings from cache")
    else:
        print("  No ANTHROPIC_API_KEY and no cached output. Set the key and retry.")
        sys.exit(1)

    # Generate HTML reports (premium web view + print-to-PDF)
    from report_template import save_html_report

    print("\n📄 Generating reports...")
    paid_html = OUTPUT_DIR / "audit_report_FULL.html"
    free_html = OUTPUT_DIR / "audit_report_FREE.html"

    save_html_report(audit_result, summaries, str(paid_html), is_paid=True)
    save_html_report(audit_result, summaries, str(free_html), is_paid=False)

    # Also generate PDF versions
    paid_pdf = OUTPUT_DIR / "audit_report_FULL.pdf"
    free_pdf = OUTPUT_DIR / "audit_report_FREE.pdf"
    build_pdf(audit_result, summaries, str(paid_pdf), is_paid=True)
    build_pdf(audit_result, summaries, str(free_pdf), is_paid=False)

    # Print summary
    findings = audit_result.get("findings", [])
    print(f"\n{'='*60}")
    print(f"  AUDIT COMPLETE")
    print(f"{'='*60}")
    print(f"Score: {audit_result.get('overallScore', '?')}/100")
    print(f"Findings: {len(findings)} total")
    print(f"  Critical: {sum(1 for f in findings if f.get('severity')=='critical')}")
    print(f"  Warning:  {sum(1 for f in findings if f.get('severity')=='warning')}")
    print(f"  Info:     {sum(1 for f in findings if f.get('severity')=='info')}")
    print(f"\n  FULL report:  {paid_html}")
    print(f"  FREE report:  {free_html}")
    print(f"  Raw JSON:     {output_json_path}")
    print(f"\n  (Open the HTML files in your browser. Cmd+P to save as PDF.)")
    print()


if __name__ == "__main__":
    main()

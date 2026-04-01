# Amazon Watchdog — Complete Technical Planning Document

**Version:** 1.1 | **Date:** 2026-04-01 | **Status:** Awaiting Approval

## Context

Amazon sellers paying agencies €500–2,000/month have no independent way to verify their agency's performance. Amazon Watchdog lets a seller upload 4 CSV reports from Seller Central and receives a cross-report analysis PDF by email. The core differentiator is cross-report correlation — findings that only emerge when combining PPC, inventory, sales, and return data simultaneously.

**Target market:** All Amazon marketplaces (not DACH-only). UI language is German initially, but architecture supports multi-marketplace from Day 1 (flexible currency, EN/DE column maps already included, marketplace-agnostic analysis logic). Column maps for additional languages (FR, IT, ES, etc.) can be added without architectural changes.

This document covers all 10 planning sections requested. No code will be written until this plan is approved.

### Key Decisions (Confirmed)
- **CSV deletion:** Immediately after processing (not 24h) — GDPR + trust
- **Ollama:** Dev-only enhancement. Production uses deterministic TypeScript summarization.
- **Monthly audit v1:** Email reminder to re-upload. Full automation via SP-API in v2.
- **Payments:** Card only (no SEPA). Stripe Checkout with EUR pricing.
- **Language:** Everything in English for now (code + output). German localization later.
- **Report detection:** By file structure (column headers), NOT filename.
- **Build order:** Processing pipeline first (local CLI), frontend/auth later.
- **Production hosting:** Vercel serverless functions.

---

## 1. ARCHITECTURE DIAGRAM

### 1.1 Request Flow: CSV Upload → PDF Email

```
┌─────────────────────────────────────────────────────────────────┐
│  USER (Browser)                                                  │
│  1. Authenticate via Supabase Magic Link                         │
│  2. Upload 1-4 CSVs via drag-and-drop                           │
│  3. Click "Analyse starten"                                      │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  POST /api/upload                                                │
│  • Validate file metadata (size ≤10MB, .csv/.txt)               │
│  • Auto-detect report type from CSV headers                      │
│  • Store CSVs in Supabase Storage (bucket: "report-uploads")    │
│  • Create `audit` row (status: "pending")                        │
│  • Create `report_files` rows                                    │
│  • Return audit_id                                               │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  POST /api/audit/[auditId]/process                               │
│                                                                   │
│  STEP 1: Download CSVs from Supabase Storage                    │
│  STEP 2: Parse with PapaParse (dynamicTyping: false)            │
│  STEP 3: Normalize columns (EN/DE header mapping)               │
│  STEP 4: Repair ASINs (scientific notation recovery)            │
│  STEP 5: Validate per report-type rules                         │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  STEP 6: TypeScript Summarization (PRODUCTION PATH)     │    │
│  │  • Pure aggregation: sums, averages, groupings          │    │
│  │  • No LLM required — deterministic functions            │    │
│  │  • Output: AuditInput JSON (~10-20KB)                   │    │
│  │                                                          │    │
│  │  (Optional dev-only: Ollama qwen3:32b for validation)   │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  STEP 7: Claude API (claude-sonnet-4-6)                 │    │
│  │  • Input: AuditInput JSON only (NEVER raw CSV)          │    │
│  │  • System prompt: German audit analyst role              │    │
│  │  • Output: AuditOutput JSON (findings + recommendations)│    │
│  │  • Cost: ~€0.02–0.05 per audit                         │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                   │
│  STEP 8: Generate PDF with @react-pdf/renderer (v3.4.x)        │
│  STEP 9: Store PDF in Supabase Storage ("audit-reports")        │
│  STEP 10: Send email via Resend (signed PDF download link)      │
│  STEP 11: Delete CSVs from storage (immediate after processing) │
│  STEP 12: Update audit row (status: "completed")                │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Key Architecture Decision: Ollama Is Dev-Only

Ollama cannot run on Vercel serverless. The **production path uses deterministic TypeScript functions** for all CSV → JSON summarization. This is pure aggregation (sums, averages, groupings, percentiles) that does not require an LLM. Ollama is an optional local dev enhancement only.

### 1.3 Data Persistence Model

| Data | Storage | Retention | Notes |
|------|---------|-----------|-------|
| User account | `auth.users` + `profiles` | Permanent | RLS: own row |
| Audit metadata | `audits` table | Permanent | RLS: own audits |
| Uploaded CSVs | Storage `report-uploads` | Deleted immediately after processing | Communicated to seller for trust/GDPR |
| Generated PDFs | Storage `audit-reports` | 90 days | Signed URL (24h expiry) |
| Audit results JSON | `audit_results` table | Permanent | RLS: own results |
| Stripe subscription | `subscriptions` table | Permanent | RLS: own row |

---

## 2. DATA MODELS

### 2.1 Supabase SQL Schemas

```sql
-- EXTENSIONS
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- PROFILES TABLE (extends auth.users)
CREATE TABLE public.profiles (
    id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    full_name TEXT,
    company_name TEXT,
    stripe_customer_id TEXT UNIQUE,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'paid')),
    free_audit_used BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.profiles (id, email)
    VALUES (NEW.id, NEW.email);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- AUDITS TABLE
CREATE TYPE audit_status AS ENUM (
    'pending', 'uploading', 'processing', 'generating_pdf',
    'sending_email', 'completed', 'failed'
);

CREATE TABLE public.audits (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    status audit_status NOT NULL DEFAULT 'pending',
    error_message TEXT,
    reports_uploaded JSONB NOT NULL DEFAULT '{}',
    -- tracks which of the 4 report types were provided
    report_count INTEGER NOT NULL DEFAULT 0,
    pdf_storage_path TEXT,
    pdf_signed_url TEXT,
    pdf_signed_url_expires_at TIMESTAMPTZ,
    email_sent_at TIMESTAMPTZ,
    processing_started_at TIMESTAMPTZ,
    processing_completed_at TIMESTAMPTZ,
    processing_duration_ms INTEGER,
    claude_tokens_used INTEGER,
    claude_cost_cents INTEGER,
    is_paid_audit BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audits_user_id ON public.audits(user_id);
CREATE INDEX idx_audits_status ON public.audits(status);
CREATE INDEX idx_audits_created_at ON public.audits(created_at DESC);

-- REPORT FILES TABLE
CREATE TYPE report_type AS ENUM (
    'business_report', 'search_term_report',
    'inventory_health', 'customer_returns'
);

CREATE TABLE public.report_files (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    audit_id UUID NOT NULL REFERENCES public.audits(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    report_type report_type NOT NULL,
    original_filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    row_count INTEGER,
    column_count INTEGER,
    validation_status TEXT DEFAULT 'pending'
        CHECK (validation_status IN ('pending', 'valid', 'invalid', 'warning')),
    validation_errors JSONB DEFAULT '[]',
    parsed_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_report_files_audit_id ON public.report_files(audit_id);
CREATE UNIQUE INDEX idx_report_files_audit_type
    ON public.report_files(audit_id, report_type);

-- AUDIT RESULTS TABLE
CREATE TABLE public.audit_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    audit_id UUID NOT NULL REFERENCES public.audits(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    summary_stats JSONB NOT NULL DEFAULT '{}',
    findings JSONB NOT NULL DEFAULT '[]',
    total_findings INTEGER NOT NULL DEFAULT 0,
    critical_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    info_count INTEGER NOT NULL DEFAULT 0,
    preprocessed_input JSONB,
    claude_raw_response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_audit_results_audit_id ON public.audit_results(audit_id);

-- SUBSCRIPTIONS TABLE
CREATE TABLE public.subscriptions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    stripe_subscription_id TEXT UNIQUE NOT NULL,
    stripe_price_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'active', 'past_due', 'canceled', 'incomplete',
        'incomplete_expired', 'trialing', 'unpaid', 'paused'
    )),
    current_period_start TIMESTAMPTZ NOT NULL,
    current_period_end TIMESTAMPTZ NOT NULL,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    canceled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_subscriptions_user_id ON public.subscriptions(user_id);

-- ROW LEVEL SECURITY
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audits ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.report_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own profile"
    ON public.profiles FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users can update own profile"
    ON public.profiles FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "Users can view own audits"
    ON public.audits FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can create own audits"
    ON public.audits FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Service role can update audits"
    ON public.audits FOR UPDATE USING (true);
    -- Processing pipeline uses service role key
CREATE POLICY "Users can view own report files"
    ON public.report_files FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can create own report files"
    ON public.report_files FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can view own audit results"
    ON public.audit_results FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Service role can insert audit results"
    ON public.audit_results FOR INSERT WITH CHECK (true);
CREATE POLICY "Users can view own subscription"
    ON public.subscriptions FOR SELECT USING (auth.uid() = user_id);
```

### 2.2 TypeScript Interfaces — CSV Row Types

```typescript
// src/types/csv-reports.ts

export interface BusinessReportRow {
  parentAsin: string;
  childAsin: string;
  title: string;
  sessions: number;
  sessionPercentage: number;
  pageViews: number;
  pageViewsPercentage: number;
  buyBoxPercentage: number;
  unitsOrdered: number;
  unitsOrderedB2B: number;
  unitSessionPercentage: number;  // conversion rate
  unitSessionPercentageB2B: number;
  orderedProductSales: number;    // revenue in EUR
  orderedProductSalesB2B: number;
  totalOrderItems: number;
  totalOrderItemsB2B: number;
}

export interface SearchTermReportRow {
  date: string;
  campaignName: string;
  adGroupName: string;
  targeting: string;
  matchType: string;          // 'BROAD' | 'PHRASE' | 'EXACT'
  customerSearchTerm: string;
  impressions: number;
  clicks: number;
  ctr: number;
  cpc: number;
  spend: number;
  sales7d: number;
  acos7d: number;
  roas7d: number;
  orders7d: number;
  units7d: number;
  conversionRate7d: number;
}

export interface InventoryHealthRow {
  snapshotDate: string;
  sku: string;
  fnsku: string;
  asin: string;
  productName: string;
  condition: string;
  availableQuantity: number;
  pendingRemovalQuantity: number;
  invAge0To90Days: number;
  invAge91To180Days: number;
  invAge181To270Days: number;
  invAge271To365Days: number;
  invAge365PlusDays: number;
  unitsSoldLast30Days: number;
  unitsSoldLast90Days: number;
  weeksOfCoverT30: number;
  weeksOfCoverT90: number;
  sellThroughRate: number;
  excessUnits: number;
  estimatedExcessStorageCostPerUnit: number;
  estimatedTotalExcessStorageCost: number;
}

export interface CustomerReturnRow {
  returnDate: string;
  orderId: string;
  sku: string;
  asin: string;
  fnsku: string;
  productName: string;
  quantity: number;
  fulfillmentCenterId: string;
  detailedDisposition: string;
  reason: string;
  status: string;
  licensePlateNumber: string;
  customerComments: string;
}
```

### 2.3 TypeScript Interfaces — Pre-Processed JSON (Input to Claude)

```typescript
// src/types/audit-input.ts

/** The ONLY data structure Claude ever sees. Raw CSV is never sent. */
export interface AuditInput {
  metadata: AuditMetadata;
  businessReport: BusinessReportSummary | null;
  searchTermReport: SearchTermReportSummary | null;
  inventoryHealth: InventoryHealthSummary | null;
  customerReturns: CustomerReturnsSummary | null;
}

export interface AuditMetadata {
  auditId: string;
  reportsProvided: string[];   // e.g. ['business_report', 'search_term_report']
  reportsMissing: string[];    // e.g. ['inventory_health', 'customer_returns']
  analysisDate: string;        // ISO date
  currency: string;            // 'EUR'
  marketplace: string;         // 'amazon.de'
}

export interface BusinessReportSummary {
  totalRevenue: number;
  totalUnitsOrdered: number;
  totalSessions: number;
  averageConversionRate: number;
  topAsinsByRevenue: Array<{
    asin: string; title: string; revenue: number;
    revenuePercentage: number; units: number;
    sessions: number; conversionRate: number; buyBoxPercentage: number;
  }>;  // top 20
  lowBuyBoxAsins: Array<{
    asin: string; title: string; buyBoxPercentage: number;
    revenue: number; sessions: number;
  }>;  // buy box < 85%
  highConversionAsins: Array<{
    asin: string; title: string; conversionRate: number;
    sessions: number; revenue: number;
  }>;  // conversion > 8%
  revenueConcentration: {
    topAsinRevenue: number;
    topAsinPercentage: number;
    topAsin: string;
    herfindahlIndex: number;
  };
}

export interface SearchTermReportSummary {
  totalSpend: number;
  totalSales: number;
  overallAcos: number;
  totalClicks: number;
  totalImpressions: number;
  wastedSpend: Array<{
    searchTerm: string; clicks: number; spend: number;
    impressions: number; campaignName: string;
  }>;  // 10+ clicks, 0 orders
  highAcosTerms: Array<{
    searchTerm: string; acos: number; spend: number;
    sales: number; clicks: number; campaignName: string;
  }>;  // ACOS > 150%
  topPerformingTerms: Array<{
    searchTerm: string; sales: number; spend: number;
    acos: number; orders: number;
  }>;  // top 10 by sales
  matchTypeDistribution: {
    exact: { spend: number; sales: number; acos: number };
    phrase: { spend: number; sales: number; acos: number };
    broad: { spend: number; sales: number; acos: number };
  };
  asinAdSpend: Record<string, number>;  // ASIN → total ad spend
}

export interface InventoryHealthSummary {
  totalSkus: number;
  totalAvailableUnits: number;
  lowStockAsins: Array<{
    asin: string; productName: string; weeksOfCover: number;
    availableQuantity: number; unitsSoldLast30Days: number;
    estimatedDaysUntilStockout: number;
  }>;  // < 2 weeks cover
  excessInventoryAsins: Array<{
    asin: string; productName: string; excessUnits: number;
    weeksOfCover: number; estimatedExcessStorageCost: number;
  }>;  // > 26 weeks cover
  totalExcessStorageCost: number;
  asinStockLevels: Record<string, {
    weeksOfCover: number;
    availableQuantity: number;
    unitsSoldLast30Days: number;
  }>;
}

export interface CustomerReturnsSummary {
  totalReturns: number;
  totalUnitsReturned: number;
  returnsByAsin: Array<{
    asin: string; productName: string; unitsReturned: number;
    returnRate: number | null;  // null if business report not provided
    topReasons: string[];
    customerCommentSummary: string[];  // keyword extraction, not LLM
  }>;
  returnReasonBreakdown: Array<{
    reason: string; count: number; percentage: number;
  }>;
  notAsDescribedRate: number;  // percentage of returns with reason "not as described"
  asinReturnRates: Record<string, {
    unitsReturned: number;
    returnRate: number | null;
    topReasons: string[];
  }>;
}
```

### 2.4 TypeScript Interfaces — Claude Structured Output

```typescript
// src/types/audit-output.ts

export interface AuditOutput {
  overallScore: number;              // 0-100
  executiveSummary: string;          // German, 2-3 sentences
  findings: Finding[];               // ordered by severity then EUR impact
  recommendations: Recommendation[];  // top 5 prioritized
  dataQualityNotes: string[];        // what couldn't be analyzed and why
}

export interface Finding {
  id: string;                        // "F001", "F002", ...
  severity: 'critical' | 'warning' | 'info';
  category: FindingCategory;
  titleDe: string;                   // German title
  descriptionDe: string;             // German explanation
  affectedAsins: string[];
  dataPoints: DataPoint[];           // evidence
  estimatedImpactEur: number | null; // monthly EUR impact estimate
  actionItemDe: string;             // question for seller to ask agency
  requiresReports: string[];        // which reports were needed
}

export type FindingCategory =
  | 'ppc_auf_defektes_produkt'
  | 'budget_auf_ausverkauftes_produkt'
  | 'fehlendes_exact_match'
  | 'klumpenrisiko'
  | 'verschwendete_werbeausgaben'
  | 'hoher_acos'
  | 'buy_box_verlust'
  | 'ueberbestand'
  | 'hohe_retourenquote'
  | 'listing_problem';

export interface DataPoint {
  label: string;    // German label
  value: string;    // formatted: "1.234,56 EUR" or "23,4%"
  context: string;  // benchmark or explanation
}

export interface Recommendation {
  priority: number;          // 1 = most important
  titleDe: string;
  descriptionDe: string;
  estimatedSavingsEur: number | null;
}
```

---

## 3. CSV PARSING STRATEGY

### 3.1 Library: PapaParse

**Critical config: `dynamicTyping: false`** — prevents ASIN corruption. PapaParse with dynamic typing converts large numeric strings to JavaScript numbers, losing precision. ASIN `3100000091` would be silently corrupted. Scientific notation values like `3.1E+09` from Excel would be mishandled.

All numeric conversions happen post-parse in the normalization layer with explicit type coercion.

### 3.2 Amazon CSV Format Quirks

**Quirk 1 — Scientific notation on ASINs.** When sellers open CSVs in Excel before uploading, purely numeric ASINs get converted to scientific notation. Recovery logic:

```typescript
// src/lib/csv/asin-repair.ts
const SCIENTIFIC_NOTATION_REGEX = /^\d+\.?\d*[eE]\+?\d+$/;

export function repairAsin(value: string): string {
  if (!value) return value;
  const trimmed = value.trim();
  if (SCIENTIFIC_NOTATION_REGEX.test(trimmed)) {
    const num = Number(trimmed);
    if (!isNaN(num) && num > 0) {
      return Math.round(num).toString().padStart(10, '0');
    }
  }
  return trimmed;
}
```

**Quirk 2 — German marketplace column name localization.** Amazon Seller Central reports can have German headers. The parser must map both English and German headers to normalized keys.

Key German↔English column mappings:

| English | German |
|---------|--------|
| sessions | Sitzungen |
| page views | Seitenaufrufe |
| buy box percentage | Buy Box-Anteil |
| units ordered | Bestellte Einheiten |
| ordered product sales | Bestellter Produktumsatz |
| unit session percentage | Einheiten-Sitzungs-Prozentsatz |
| campaign name | Kampagnenname |
| customer search term | Suchbegriff des Kunden |
| spend | Ausgaben |
| product name | Produktname / Artikelbezeichnung |
| reason | Grund |
| quantity | Menge |
| return date | Rückgabedatum |
| weeks of cover | Wochen mit Versorgung |
| excess units | Überbestand Einheiten |

Full mapping lives in `src/lib/csv/column-maps.ts` with all 4 report types.

**Quirk 3 — Encoding issues.** Amazon CSVs may be UTF-8, Windows-1252, or ISO-8859-1. Strategy:
1. Attempt UTF-8 parse
2. Detect mojibake patterns (e.g. `Ã¼` instead of `ü`)
3. If detected, re-parse as Windows-1252
4. Implementation in `src/lib/csv/encoding-detection.ts`

**Quirk 4 — Header row offset.** Some reports include metadata rows before the actual CSV header. Scan first 5 lines, identify header by matching against known column maps, discard preceding lines.

**Quirk 5 — European number formatting.** German-locale reports use comma as decimal separator (`1.234,56` vs `1,234.56`). Detect pattern from first numeric values, apply appropriate conversion in `src/lib/csv/number-parser.ts`.

### 3.3 Validation Rules Per Report Type

Required columns (minimum set, using normalized English keys):

| Report | Required Columns | Max Rows |
|--------|-----------------|----------|
| business_report | childAsin, sessions, buyBoxPercentage, unitsOrdered, orderedProductSales, unitSessionPercentage | 50,000 |
| search_term_report | campaignName, matchType, customerSearchTerm, impressions, clicks, spend, sales7d, orders7d | 500,000 |
| inventory_health | asin, productName, availableQuantity, unitsSoldLast30Days, weeksOfCoverT30, excessUnits | 50,000 |
| customer_returns | returnDate, asin, productName, quantity, reason | 100,000 |

File size limit: 10MB per file. Total upload limit: 40MB.

### 3.4 Report Type Auto-Detection

Rather than requiring user labels, auto-detect by scoring CSV header matches against all 4 column maps. The type with the highest match score (minimum 3 column matches) wins. If ambiguous, show the detection result and let the user confirm. Implemented in `src/lib/csv/report-detector.ts`.

### 3.5 Handling Partial Uploads

- **Minimum:** 1 report required to run an audit
- Cross-report findings only generated when both required reports are present
- `AuditInput.metadata.reportsMissing` communicates gaps to Claude
- Claude's `dataQualityNotes` explains what analysis was impossible
- PDF marks sections that couldn't be analyzed

Cross-report finding requirements:

| Finding | Requires |
|---------|----------|
| PPC auf defektes Produkt | search_term_report + customer_returns |
| Budget auf ausverkauftes Produkt | search_term_report + inventory_health |
| Fehlendes Exact-Match | business_report + search_term_report |
| Klumpenrisiko | business_report only |

---

## 4. LOCAL LLM PROMPT DESIGNS

### 4.1 Production Architecture: No LLM for Summarization

CSV → JSON summarization is **deterministic TypeScript** in production. The aggregation operations (sum, average, group-by, sort, filter, percentile) don't require reasoning. This eliminates Ollama as a production dependency.

### 4.2 Summarization Functions (Production Path)

All live in `src/lib/analysis/`:

- **`summarize-business-report.ts`** — Computes total revenue, units, sessions, average conversion; ranks ASINs by revenue (top 20); flags buy box < 85%; flags conversion > 8%; computes revenue concentration (Herfindahl index).

- **`summarize-search-terms.ts`** — Computes total spend/sales/ACOS; filters wasted spend (10+ clicks, 0 orders); filters high ACOS (>150%); builds match type distribution; maps per-ASIN ad spend; identifies campaigns with/without exact match.

- **`summarize-inventory.ts`** — Flags low stock (<2 weeks cover); flags excess inventory (>26 weeks cover); totals excess storage costs; maps per-ASIN stock levels.

- **`summarize-returns.ts`** — Groups returns by ASIN; calculates return rates (cross-referencing units ordered from business report if available); clusters return reasons; computes "not as described" rate; extracts keyword themes from customer comments (substring matching, not LLM).

- **`build-audit-input.ts`** — Combines all summaries into a single `AuditInput` object, setting null for missing reports.

### 4.3 Ollama Prompts (Dev-Only Enhancement)

If Ollama is running locally (detected via `GET http://localhost:11434/api/tags` with 2s timeout), it can optionally enhance validation with natural-language error messages. This is **not** in the critical path.

```
Du bist ein Datenvalidierungs-Assistent für Amazon Seller Central Berichte.

Berichtstyp: {{REPORT_TYPE}}
Anzahl Zeilen: {{ROW_COUNT}}
Erkannte Spalten: {{DETECTED_COLUMNS}}
Erwartete Spalten: {{EXPECTED_COLUMNS}}
Fehlende Spalten: {{MISSING_COLUMNS}}
Erste 3 Zeilen (als JSON): {{SAMPLE_ROWS}}

Antworte NUR als JSON:
{
  "isValid": boolean,
  "errors": [{"code": "...", "messageDe": "..."}],
  "warnings": [{"code": "...", "messageDe": "..."}],
  "dataQuality": "good" | "acceptable" | "poor",
  "qualityNote": "kurze Beschreibung auf Deutsch"
}
```

Config: model `qwen3:32b`, temperature 0.1, max_tokens 4096, timeout 120s.

### 4.4 Fallback Strategy

If Ollama is unavailable (dev): TypeScript summarization runs anyway (it's the primary path). Validation uses hardcoded German error messages instead of LLM-generated ones. No user-visible difference.

---

## 5. CLAUDE API PROMPT DESIGN

### 5.1 System Prompt

```
Du bist ein erfahrener Amazon-Marktplatz-Analyst, der Seller dabei hilft,
die Arbeit ihrer Amazon-Agentur zu überprüfen.

Deine Aufgabe:
- Analysiere die vorverarbeiteten Daten aus Amazon Seller Central Berichten
- Identifiziere Probleme, verschwendetes Budget und verpasste Chancen
- Bewerte, ob die Agentur des Sellers gute Arbeit leistet
- Alle Ausgaben MÜSSEN auf Deutsch sein

Analyse-Schwerpunkte (in Prioritätsreihenfolge):

CROSS-REPORT BEFUNDE (höchste Priorität — nur wenn beide Berichte vorhanden):
1. Hohe PPC-Ausgaben + hohe Retourenquote >10% bei gleicher ASIN
   → "Werbung auf defektes Produkt" (severity: critical)
2. <2 Wochen Lagerbestand + steigende Werbeausgaben bei gleicher ASIN
   → "Agentur erhöht Budget auf Produkt das bald ausverkauft ist" (critical)
3. Hohe Conversion >8% + keine Exact-Match-Kampagnen für diese ASIN
   → "Gute Performance — Agentur hat kein Exact-Match aufgesetzt" (warning)
4. Ein ASIN >60% des Gesamtumsatzes
   → "Zu hohes Klumpenrisiko" (warning)

EINZELBERICHT-BEFUNDE:
5. Suchbegriffe mit 10+ Klicks, 0 Conversions → verschwendetes Budget in EUR
6. ACOS >150% bei spezifischen Suchbegriffen → verschwendetes Budget in EUR
7. Buy Box Anteil unter 85% → potentieller Umsatzverlust
8. Überbestand >26 Wochen Reichweite → Lagergebühren-Risiko
9. Retourenquote pro ASIN im Verhältnis zu bestellten Einheiten
10. Retouren-Grund "nicht wie beschrieben" >1,5% → Listing-Problem

Regeln:
- Berechne keine eigenen Zahlen; verwende NUR die bereitgestellten Datenpunkte
- Wenn ein Bericht fehlt, überspringe die betreffenden Befunde und
  notiere das unter dataQualityNotes
- Sortiere Befunde: erst Schweregrad (critical > warning > info),
  dann geschätzter EUR-Impact absteigend
- Jeder Befund MUSS einen konkreten actionItemDe enthalten — formuliert
  als Frage, die der Seller seiner Agentur stellen kann
- EUR-Format: Punkt als Tausendertrennzeichen, Komma als Dezimalzeichen
  (z.B. "1.234,56 EUR")
- overallScore: 0-30 schwerwiegend, 31-50 deutliche Mängel,
  51-70 Verbesserungspotential, 71-85 solide, 86-100 hervorragend

Antworte AUSSCHLIESSLICH mit validem JSON gemäß dem bereitgestellten Schema.
```

### 5.2 User Prompt Template

```typescript
// src/lib/llm/claude-prompts.ts

export function buildClaudeUserPrompt(input: AuditInput): string {
  const missingNote = input.metadata.reportsMissing.length > 0
    ? `\n\nACHTUNG: Folgende Berichte fehlen: ${input.metadata.reportsMissing.join(', ')}. ` +
      `Cross-Report-Befunde die diese Berichte erfordern können NICHT erstellt werden.`
    : '';

  return `Analysiere die folgenden vorverarbeiteten Amazon Seller Central Daten.

Audit-ID: ${input.metadata.auditId}
Analysedatum: ${input.metadata.analysisDate}
Marktplatz: ${input.metadata.marketplace}
Bereitgestellte Berichte: ${input.metadata.reportsProvided.join(', ')}${missingNote}

DATEN:
${JSON.stringify(input, null, 2)}

Antworte als JSON mit folgendem Schema:
{
  "overallScore": number (0-100),
  "executiveSummary": string (2-3 Sätze auf Deutsch),
  "findings": [
    {
      "id": string ("F001"),
      "severity": "critical" | "warning" | "info",
      "category": string,
      "titleDe": string,
      "descriptionDe": string,
      "affectedAsins": string[],
      "dataPoints": [{"label": string, "value": string, "context": string}],
      "estimatedImpactEur": number | null,
      "actionItemDe": string,
      "requiresReports": string[]
    }
  ],
  "recommendations": [
    {
      "priority": number (1 = wichtigste),
      "titleDe": string,
      "descriptionDe": string,
      "estimatedSavingsEur": number | null
    }
  ],
  "dataQualityNotes": string[]
}`;
}
```

### 5.3 API Configuration

- Model: `claude-sonnet-4-6`
- Temperature: 0.2 (low for consistent structured output)
- Max tokens: 8,192
- Response parsing: strip optional markdown code fences before `JSON.parse()`
- Retry: 1 retry on 5xx or timeout, exponential backoff
- Timeout: 60 seconds

### 5.4 Handling Incomplete Data

| Reports Uploaded | Available Analysis |
|-----------------|-------------------|
| All 4 | Full cross-report + single-report |
| 3 of 4 | Cross-reports where both sources present + all single-report for uploaded |
| 2 of 4 | Limited cross-reports + single-report for uploaded |
| 1 of 4 | Single-report findings only + "incomplete" warning |

Claude receives explicit `reportsMissing` list and is instructed to skip impossible findings.

### 5.5 Cost Estimate

- Input: ~5,000 tokens (pre-processed JSON)
- Output: ~3,000 tokens (findings JSON)
- Per-audit: ~€0.02–0.05
- 1,000 audits/month: ~€20–50

---

## 6. UI FLOW

### 6.1 Page Structure (Next.js App Router)

```
app/
  layout.tsx                     -- root layout, lang="de"
  page.tsx                       -- landing page (SSG)
  login/page.tsx                 -- magic link auth
  impressum/page.tsx             -- legal: imprint
  datenschutz/page.tsx           -- legal: privacy policy
  dashboard/
    layout.tsx                   -- authenticated layout
    page.tsx                     -- audit history list
  audit/
    new/page.tsx                 -- upload wizard
    [auditId]/
      page.tsx                   -- processing status → results
      pdf/route.ts               -- PDF download endpoint
  api/
    auth/callback/route.ts       -- Supabase auth callback
    upload/route.ts              -- CSV upload + validation
    audit/[auditId]/
      process/route.ts           -- main processing pipeline
      status/route.ts            -- polling endpoint
    stripe/
      checkout/route.ts          -- create checkout session
      webhook/route.ts           -- handle Stripe events
      portal/route.ts            -- customer portal redirect
```

### 6.2 Screen: Landing Page (`/`)

**Headline:** "Macht Ihre Amazon-Agentur wirklich guten Job?"

**Subheadline:** "Laden Sie 4 Berichte aus Seller Central hoch. Erhalten Sie in 2 Minuten eine unabhängige Analyse per E-Mail."

**Primary CTA:** "Gratis Audit starten" → `/login` (then redirect to `/audit/new`)

**3 Value Proposition Columns:**
1. "Cross-Report Analyse" — Verknüpfung von Werbe-, Bestands-, Umsatz- und Retouren-Daten
2. "Konkrete EUR-Beträge" — Echte Euro-Beträge für verschwendetes Budget
3. "In 2 Minuten fertig" — Upload, warten, PDF per E-Mail

**Social proof section:** testimonial placeholders (fill after first users)

**Footer:** Impressum, Datenschutzerklärung links (legally required in Germany)

### 6.3 Screen: Upload Wizard (`/audit/new`)

Four upload cards arranged vertically, each containing:
- Report type name in German with icon
- Collapsible step-by-step Seller Central navigation instructions
- Drag-and-drop zone with "CSV-Datei hierher ziehen oder klicken"
- After upload: green checkmark + detected report type + row count
- If validation fails: red error with German explanation

**Navigation instructions per report (in German):**

1. **Geschäftsbericht:**
   "Seller Central → Berichte → Geschäftsberichte → Detailseite: Umsatz und Traffic (nach untergeordneter ASIN) → Zeitraum: Letzte 30 Tage → Herunterladen"

2. **Suchbegriffbericht (Sponsored Products):**
   "Seller Central → Werbung → Kampagnenmanager → Berichte → Bericht erstellen → Berichtstyp: Suchbegriff → Zeitraum: Letzte 30 Tage → Herunterladen"

3. **FBA-Lagerbestandsbericht:**
   "Seller Central → Lagerbestand → Lagerbestandsplanung → FBA-Lagerbestand → Herunterladen"

4. **FBA-Retourenbericht:**
   "Seller Central → Berichte → Versand durch Amazon → Kundenrücksendungen → Zeitraum: Letzte 30 Tage → Bericht generieren"

**Bottom section:**
- Indicator: "X von 4 Berichten hochgeladen" (minimum 1 required)
- "Analyse starten" button (disabled until ≥1 valid report)
- Note: "Je mehr Berichte Sie hochladen, desto umfassender die Analyse."
- Trust signal: "Ihre Daten werden nach der Analyse sofort gelöscht."

### 6.4 Screen: Processing (`/audit/[auditId]`)

Animated stepper showing progression:
1. ✅/⏳ "CSV-Dateien werden validiert..."
2. ✅/⏳ "Daten werden analysiert..."
3. ✅/⏳ "Bericht wird erstellt..."
4. ✅/⏳ "E-Mail wird versendet..."

Polls `GET /api/audit/[auditId]/status` every 2 seconds. Auto-transitions to results view on completion.

If processing fails: show German error message + "Erneut versuchen" button.

### 6.5 Screen: Results + Paywall (`/audit/[auditId]`)

**Header:** Overall score gauge (0–100, color-coded), executive summary paragraph.

**Findings list:**
- **Free tier:** Top 3 findings shown fully (title, description, data points, action item). Remaining findings show title + severity badge only, descriptions blurred with CSS backdrop-filter.
- **Upgrade CTA overlay:** "Sie sehen 3 von X Befunden. Schalten Sie alle Befunde frei für nur 29 €/Monat."
- **Paid tier:** All findings shown with full details, data points, estimated EUR impact, and action items.

**Actions:**
- "PDF herunterladen" button (free: 3-finding PDF, paid: full PDF)
- "Alle Befunde freischalten" → Stripe checkout (free tier only)

### 6.6 Rate Limiting & Abuse Prevention

- 1 free audit per email address (lifetime, tracked in `profiles.free_audit_used`)
- 3 audits per IP per 24 hours (rate_limits table)
- 10 file uploads per IP per hour
- 1 concurrent processing job per user
- Honeypot field on upload form (hidden input, reject if filled)
- File size limit: 10MB per file, 40MB total

---

## 7. PDF REPORT STRUCTURE

### 7.1 Technology: @react-pdf/renderer

**Pin to v3.4.x with React 18.3.x.** Version 4.x has known breaking changes with React 19 and Vercel serverless. Add `@react-pdf/renderer` to `serverComponentsExternalPackages` in `next.config.js`.

Generate PDFs in a dedicated API route (`/audit/[auditId]/pdf/route.ts`), not a server component.

**Fallback plan:** If @react-pdf/renderer proves unstable on Vercel, switch to `pdfkit` (pure Node.js, no React dependency, no browser needed).

### 7.2 Section Order & German Headings

1. **DECKBLATT** (Cover Page)
   - "Amazon Watchdog — Agentur-Audit"
   - Company name (if provided), audit date, analysis period
   - Overall score: large color-coded circle (red/yellow/green) with number
   - Score interpretation text

2. **ZUSAMMENFASSUNG** (Executive Summary)
   - Claude's `executiveSummary` paragraph
   - Key metrics grid:
     - Gesamtumsatz (total revenue)
     - Werbeausgaben (ad spend)
     - Gesamt-ACOS
     - Analysierte ASINs
     - Anzahl Befunde nach Schweregrad

3. **BEFUNDE** (Findings)
   - Each finding as a card:
     - Severity badge: 🔴 KRITISCH / 🟡 WARNUNG / 🔵 INFO
     - `titleDe`
     - `descriptionDe`
     - Affected ASINs list
     - Data points table (label | value | context)
     - Estimated EUR impact (highlighted)
     - `actionItemDe` — "Fragen Sie Ihre Agentur:" box
   - Sorted: critical first, then by EUR impact descending

4. **EMPFEHLUNGEN** (Recommendations)
   - Top 5 prioritized next steps
   - Each with title, description, estimated savings

5. **HINWEISE** (Notes & Disclaimers)
   - Data quality notes (what couldn't be analyzed)
   - "Diese Analyse basiert auf den bereitgestellten Daten und stellt keine Geschäftsberatung dar."
   - "Ihre CSV-Daten wurden nach der Analyse sofort gelöscht."
   - Amazon Watchdog branding + website

### 7.3 Free vs Paid PDF Content

| Section | Free | Paid |
|---------|------|------|
| Cover + Score | ✅ Full | ✅ Full |
| Executive Summary | ✅ Full | ✅ Full |
| Findings | First 3 only | All findings |
| After finding 3 | Upgrade CTA block | (not shown) |
| Recommendations | ❌ Hidden | ✅ Full |
| Notes | ✅ Full | ✅ Full |

Free PDF upgrade CTA block text:
"Weitere X Befunde sind in der kostenlosen Version nicht enthalten. Schalten Sie alle Befunde frei: https://amazonwatchdog.de/upgrade — Nur 29 €/Monat."

---

## 8. STRIPE INTEGRATION

### 8.1 Product Configuration

- Product: "Amazon Watchdog Pro"
- Price: €29.00/month, recurring
- Payment methods: Card only
- Tax: Stripe automatic EU VAT (inclusive pricing), tax ID collection enabled for B2B
- Locale: `de`

### 8.2 Free Audit Flow

1. User authenticates via magic link
2. Check `profiles.free_audit_used`
3. If `false`: allow audit, set `free_audit_used = true` after completion, generate PDF with 3 findings
4. If `true`: show "Sie haben Ihr kostenloses Audit bereits verwendet" + upgrade CTA
5. Paid users: unlimited audits

### 8.3 Checkout Session (`POST /api/stripe/checkout`)

```typescript
const session = await stripe.checkout.sessions.create({
  customer: stripeCustomerId,         // get-or-create by Supabase user
  mode: 'subscription',
  line_items: [{ price: STRIPE_PRICE_ID, quantity: 1 }],
  payment_method_types: ['card'],
  locale: 'de',
  allow_promotion_codes: true,
  automatic_tax: { enabled: true },
  tax_id_collection: { enabled: true },
  success_url: `${APP_URL}/dashboard?upgraded=true`,
  cancel_url: `${APP_URL}/audit/${auditId}`,
  metadata: { supabase_user_id: userId },
});
```

### 8.4 Webhook Handler (`POST /api/stripe/webhook`)

Events to handle:

| Event | Action |
|-------|--------|
| `checkout.session.completed` | Set `profiles.tier = 'paid'`, create `subscriptions` row |
| `invoice.payment_succeeded` | Update subscription period dates |
| `customer.subscription.updated` | Sync status, handle cancellation |
| `customer.subscription.deleted` | Set `profiles.tier = 'free'` |

Uses Supabase service role client (no user auth context in webhooks). Validates webhook signature via `stripe.webhooks.constructEvent()`.

### 8.5 Customer Portal

Stripe Customer Portal for self-service subscription management (cancel, update payment method, view invoices). Redirect via `POST /api/stripe/portal` → `stripe.billingPortal.sessions.create()`.

---

## 9. BUILD ORDER (14-Day Plan)

### Day 1 — Project Bootstrap + Auth
- `npx create-next-app@14` with TypeScript, Tailwind, App Router
- Configure `next.config.js` (`serverComponentsExternalPackages`)
- Create Supabase project, run all SQL migrations from Section 2
- Implement magic link auth: login page, callback route, auth middleware
- **Done when:** User can sign up with magic link and see empty dashboard

### Day 2 — CSV Parsing Layer
- Install `papaparse`
- Implement `src/lib/csv/column-maps.ts` (all 4 report types, EN + DE)
- Implement `src/lib/csv/encoding-detection.ts`
- Implement `src/lib/csv/asin-repair.ts`
- Implement `src/lib/csv/number-parser.ts`
- Implement `src/lib/csv/report-detector.ts`
- Implement `src/lib/csv/validators.ts`
- Unit tests with sample CSVs
- **Done when:** Tests pass for all 4 report types with both EN/DE headers

### Day 3 — Upload API + UI
- Create upload API route (multipart form, validate, store in Supabase Storage, create DB rows)
- Build upload wizard page with 4 report cards, drag-and-drop, German instructions
- Validation feedback and auto-detection display
- **Done when:** Files upload successfully, show in Supabase Storage, validation results displayed

### Day 4 — Data Summarization
- Implement `src/lib/analysis/summarize-business-report.ts`
- Implement `src/lib/analysis/summarize-search-terms.ts`
- Implement `src/lib/analysis/summarize-inventory.ts`
- Implement `src/lib/analysis/summarize-returns.ts`
- Implement `src/lib/analysis/build-audit-input.ts`
- Unit tests with fixture data for each summarizer
- **Done when:** Tests pass, AuditInput JSON generated correctly from test CSVs

### Day 5 — Claude API Integration
- Install `@anthropic-ai/sdk`
- Implement `src/lib/llm/claude-prompts.ts` (system + user prompt)
- Implement `src/lib/llm/claude-client.ts` (API call, JSON parse, validation with Zod)
- Create `MOCK_MODE` env var with fixture AuditOutput for development without API calls
- **Done when:** Claude returns valid AuditOutput from test AuditInput

### Day 6 — Processing Pipeline + Status UI
- Create `POST /api/audit/[auditId]/process` route (orchestrates: download → parse → summarize → Claude → store results)
- Create `GET /api/audit/[auditId]/status` polling endpoint
- Build processing UI with animated stepper, 2-second polling
- Error handling: failed status, German error messages
- **Done when:** End-to-end flow from upload to stored results (using mock or real Claude)

### Day 7 — Results Page + Paywall
- Build results page: score gauge, finding cards, data tables
- Implement free tier view: 3 findings + blur + upgrade CTA
- Implement paid tier view: all findings
- **Done when:** Results page renders correctly for both free and paid states

### Day 8 — PDF Generation
- Install `@react-pdf/renderer@3.4.x`
- Build PDF components: cover, summary, findings, recommendations, notes
- Create `GET /audit/[auditId]/pdf` download route
- Implement free vs paid content gating
- Test Vercel deployment compatibility
- **Done when:** PDF downloads correctly, renders all sections, free/paid versions differ

### Day 9 — Email Integration
- Set up Resend account + verify sending domain
- Build German HTML email template (audit complete notification)
- Generate signed PDF download URLs (24h expiry)
- Integrate email sending into processing pipeline (Step 10)
- **Done when:** User receives email with working PDF download link after audit completes

### Day 10 — Stripe Integration
- Create Stripe product + price (€29/month)
- Implement checkout route, webhook route, portal route
- Test locally with Stripe CLI (`stripe listen --forward-to`)
- Wire upgrade buttons to checkout flow
- Handle subscription lifecycle events
- **Done when:** Full payment flow works: upgrade → webhook → tier change → full results visible

### Day 11 — Landing Page + Legal Pages
- Build German landing page with all copy, value props, CTA
- Create Impressum page (legal requirement)
- Create Datenschutzerklärung page (GDPR privacy policy)
- Meta tags, Open Graph, responsive design
- **Done when:** Landing page is production-ready, legal pages complete

### Day 12 — Dashboard + Polish
- Build audit history list (status, score, date, re-download PDF)
- Profile settings page (name, company, subscription status)
- Stripe customer portal link for subscription management
- Navigation, loading states, error boundaries
- **Done when:** Full dashboard experience with history and settings

### Day 13 — Testing + Hardening
- Test with real Amazon CSVs (all 4 types, EN + DE headers)
- Test edge cases: scientific notation ASINs, encoding issues, partial uploads, large files
- German error message audit
- Rate limiting implementation and testing
- RLS security review (ensure no cross-tenant data leakage)
- Timeout handling for long processing jobs
- **Done when:** All edge cases handled, security review passed

### Day 14 — Deploy + Monitor
- Vercel production deployment with all env vars
- Supabase production project + run migrations
- Stripe production mode webhooks
- Resend production domain
- Error monitoring setup (Vercel Analytics or Sentry)
- Cron job: delete PDFs older than 90 days, clean orphaned files
- Full end-to-end smoke test in production
- **Done when:** Live at production URL, full audit flow works end-to-end

---

## 10. OPEN QUESTIONS

### Decisions Needed Before Day 1

1. **Domain name** — Needed for Vercel deployment, Resend email, Impressum. Suggestion: `amazonwatchdog.de`. Is this already registered?

2. **Legal entity** — German Impressum legally requires company name, address, and responsible person. What entity should be listed?

3. **Supabase plan** — Free tier has 500MB database and 1GB storage. Likely insufficient for production. Pro plan ($25/month) recommended. Confirm?

4. **Vercel plan** — Free tier has 10-second function timeout, insufficient for processing pipeline. Pro plan ($20/month) gives 60-second timeout. Confirm?

### Decisions Needed Before Day 5

5. **Claude model** — Spec says `claude-sonnet-4-6`. Confirm this vs Haiku (cheaper at ~€0.005/audit but less reliable for nuanced German analysis)?

### Resolved Decisions
- ~~Ollama in production~~ → Eliminated. TypeScript summarization in production. ✅
- ~~SEPA Direct Debit~~ → Not relevant. Card payments only. ✅
- ~~CSV deletion timing~~ → Immediately after processing. ✅
- ~~Monthly automated audit~~ → Email reminder in v1, SP-API in v2. ✅
- ~~DACH-only~~ → All marketplaces. Architecture supports multi-marketplace from Day 1. ✅

### Architectural Concerns to Be Aware Of

9. **Vercel function size** — @react-pdf/renderer + Anthropic SDK + PapaParse may approach 50MB. Mitigation: split PDF generation into its own serverless function.

10. **Processing timeout** — 500K-row search term reports may be slow. Mitigation: streaming PapaParse, limit to top 10,000 search terms by spend for summarization.

11. **@react-pdf/renderer stability** — v4 breaks with React 19 on Vercel. Plan pins to v3.4.x with React 18. If it proves unstable, fallback to `pdfkit`.

12. **GDPR compliance** — Requires DPAs with Supabase, Anthropic, Stripe, Resend. Privacy policy must list all processors. CSV deletion is immediate after processing (confirmed).

### Post-V1 Considerations (Not in Scope)

13. **Automated monthly audits** — Spec mentions paid tier includes this. V1 recommendation: send monthly email reminder to re-upload. Full automation requires Amazon SP-API (separate 2-4 week project).

14. **Amazon SP-API integration** — For automated report pulling, month-over-month trends.

15. **Multi-marketplace** — UK, FR, IT, ES: additional column maps, currencies, localized UI.

---

## Appendix A: Environment Variables

```env
# Supabase
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=

# AI
ANTHROPIC_API_KEY=
OLLAMA_BASE_URL=http://localhost:11434  # dev only

# Stripe
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRICE_ID=
NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=

# Email
RESEND_API_KEY=
RESEND_FROM_EMAIL=audit@amazonwatchdog.de

# App
NEXT_PUBLIC_APP_URL=https://amazonwatchdog.de
MOCK_MODE=false  # true = use fixture data instead of Claude API
```

## Appendix B: Key Dependencies

```
next@^14.2
react@^18.3
react-dom@^18.3
@supabase/supabase-js@^2.45
@supabase/ssr@^0.5
@anthropic-ai/sdk@^0.30
@react-pdf/renderer@^3.4  (NOT v4)
papaparse@^5.4
@types/papaparse@^5.3
stripe@^16
resend@^4
tailwindcss@^3.4
zod@^3.23
vitest@^2  (dev dependency)
```

## Appendix C: Critical Files (Implementation Order)

1. `src/lib/csv/column-maps.ts` — Column name mapping (EN + DE) for all 4 report types
2. `src/lib/csv/report-detector.ts` — Auto-detect report type from headers
3. `src/lib/csv/asin-repair.ts` — Scientific notation recovery
4. `src/lib/csv/encoding-detection.ts` — UTF-8 / Windows-1252 detection
5. `src/lib/csv/number-parser.ts` — European number format handling
6. `src/lib/csv/validators.ts` — Per-report validation rules
7. `src/types/csv-reports.ts` — Row interfaces for all 4 CSV types
8. `src/types/audit-input.ts` — AuditInput interface (contract: summarization → Claude)
9. `src/types/audit-output.ts` — AuditOutput interface (contract: Claude → PDF/UI)
10. `src/lib/analysis/summarize-*.ts` — 4 summarization functions
11. `src/lib/analysis/build-audit-input.ts` — Combine summaries into AuditInput
12. `src/lib/llm/claude-prompts.ts` — System + user prompt templates
13. `src/lib/llm/claude-client.ts` — API call + JSON validation
14. `src/app/api/audit/[auditId]/process/route.ts` — Main processing pipeline
15. `src/lib/pdf/audit-pdf.tsx` — PDF document components

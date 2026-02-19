# ECMAuths Email Analytics Dashboard - Professional Edition
# Loads data from SQL Server table Alliance_RPT.DataScience.ecm_mailbox
# Run with: shiny run --reload --host 127.0.0.1 --port 9000 app_mail.py
# Also viewable as: http://aadsprodsv:9000/


import json
import os
import platform
import subprocess
from datetime import datetime

import pandas as pd
from shiny import App, ui, render, reactive

from sqlalchemy import create_engine, text


# ============================================================
# DATETIME PARSING
# ============================================================

def parse_ms_json_datetime(value):
        """
        Parse Microsoft-style /Date(XXXXXXXXXXXX)/ (ms since epoch)
        or normal ISO datetime into a UTC-aware pandas.Timestamp.
        """
        if not value:
            return pd.NaT

        if isinstance(value, str) and value.startswith("/Date(") and value.endswith(")/"):
            inner = value[len("/Date("):-len(")/")]
            try:
                ms = int(inner)
                return pd.to_datetime(ms, unit="ms", utc=True)
            except Exception:
                return pd.NaT

        return pd.to_datetime(value, errors="coerce", utc=True)


# ============================================================
# CONFIG (UPDATED: SQL SERVER SOURCE)
# ============================================================

MAILBOX = "ECMAuths@thealliance.health"

KEYTAB = "/home/shiraoka/datascience.keytab"
PRINCIPAL = "datascience-srv@CCAH-ALLIANCE.ORG"

CONN_STR = (
    "mssql+pyodbc://shiraoka@RPTPRODDB/Alliance_RPT"
    "?driver=ODBC+Driver+18+for+SQL+Server"
    "&trusted_connection=yes"
    "&Encrypt=no"
)

SQL_SCHEMA = "DataScience"
SQL_TABLE = "ecm_mailbox"


# ============================================================
# SQL AUTH + LOAD
# ============================================================

def kinit_with_keytab(keytab: str, principal: str) -> None:
        """
        Acquire Kerberos ticket using keytab.
        - On Windows: no-op (typical integrated auth behavior).
        - On Linux: runs kinit if keytab exists.
        """
        if platform.system().lower().startswith("win"):
            return

        if not keytab or not principal:
            return

        if not os.path.exists(keytab):
            # Don't hard-fail; environment may already have a ticket.
            return

        subprocess.run(
            ["kinit", "-kt", keytab, principal],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def load_sql_messages() -> pd.DataFrame:
        """
        Load ECM mailbox rows from SQL Server table Alliance_RPT.DataScience.ecm_mailbox
        """
        # Kerberos (Linux)
        kinit_with_keytab(KEYTAB, PRINCIPAL)

        engine = create_engine(CONN_STR, pool_pre_ping=True)

        sql = text(f"""
            SELECT
                row_id,
                export_batch_id,
                exported_utc,
                loaded_utc,
                message_id,
                conversation_id,
                folder_name,
                subject,
                from_name,
                from_address,
                sender_name,
                sender_address,
                to_recipients_json,
                cc_recipients_json,
                sent_datetime,
                received_datetime,
                body_preview
            FROM {SQL_SCHEMA}.{SQL_TABLE}
        """)

        with engine.connect() as conn:
            df = pd.read_sql_query(sql, conn)

        return df


# ============================================================
# DATA PREP HELPERS
# ============================================================

def extract_email_address(obj) -> str | None:
        """Robustly extract an email address from a variety of possible shapes."""
        if not obj:
            return None

        if isinstance(obj, str):
            s = obj.strip()
            if "<" in s and ">" in s:
                inner = s[s.find("<") + 1:s.find(">")].strip()
                if "@" in inner:
                    return inner
            if "@" in s:
                return s
            return None

        if isinstance(obj, dict):
            for _, v in obj.items():
                if isinstance(v, dict) and "@" not in str(obj):
                    for k2, v2 in v.items():
                        if k2.lower() == "address" and isinstance(v2, str) and "@" in v2:
                            return v2

            for k, v in obj.items():
                if not isinstance(v, str):
                    continue
                if k.lower() in (
                    "address",
                    "from",
                    "sender",
                    "fromaddress",
                    "senderaddress",
                    "email",
                    "emailaddress",
                ) and "@" in v:
                    return v.strip()

            for key in obj.keys():
                if key.lower() == "emailaddress":
                    nested = obj[key]
                    if isinstance(nested, dict):
                        addr = nested.get("address") or nested.get("Address")
                        if isinstance(addr, str) and "@" in addr:
                            return addr.strip()

        return None


def list_recipients(recips) -> list[str]:
        """Extract list of lowercased email addresses from recipients collection."""
        if not recips:
            return []

        if not isinstance(recips, list):
            recips = [recips]

        out: list[str] = []
        for r in recips:
            if isinstance(r, dict) and "emailAddress" in r:
                addr = extract_email_address(r["emailAddress"])
            else:
                addr = extract_email_address(r)
            if addr:
                out.append(addr.lower())
        return out


def parse_recipients_json(value) -> list[str]:
        """
        SQL columns to_recipients_json / cc_recipients_json are expected to be JSON strings.
        Convert them to list[str] (email addresses).
        """
        if value is None:
            return []
        if isinstance(value, list):
            return list_recipients(value)
        if isinstance(value, dict):
            return list_recipients([value])

        s = str(value).strip()
        if not s:
            return []

        try:
            obj = json.loads(s)
        except Exception:
            # best-effort fallback: try to parse as raw string
            return list_recipients(s)

        return list_recipients(obj)


def classify_subject(subject: str) -> str:
        """Keyword-based subject classification (expanded to reduce 'Other')."""
        if not subject:
            return "Other"

        s = subject.lower().strip()

        rules = [
            (["urgent", "asap", "stat", "time sensitive", "escalat", "follow up", "follow-up"], "Urgent / Escalation"),
            (["fyi", "for your information"], "FYI / Informational"),

            (["prior auth", "preauth", "pre-auth", "authorization", "auth req", "auth request", "auth "], "Authorization"),
            (["retro", "retro auth", "retroactive"], "Retro Authorization"),

            (["status", "update", "checking on", "any update", "pending"], "Status Update"),
            (["inquiry", "question", "clarification"], "Inquiry / Clarification"),

            (["denial", "denied", "adverse", "not approved", "unable to approve"], "Denial"),
            (["approval", "approved", "authorized"], "Approval"),
            (["appeal", "reconsideration", "grievance"], "Appeal / Grievance"),

            (["medical record", "medical records", "chart", "notes", "clinical", "documentation", "supporting"], "Clinical Documentation"),
            (["fax", "fwd:", "forwarded", "scan", "scanned"], "Fax / Forwarded Item"),

            (["member", "patient", "enrollee"], "Member Related"),
            (["provider", "office", "clinic", "facility", "hospital", "dr.", "doctor"], "Provider Related"),

            (["appointment", "schedule", "reschedule", "availability"], "Scheduling"),
            (["referral", "referred", "ra ", "r/a "], "Referral"),

            (["invoice", "bill", "billing", "payment", "paid", "refund"], "Billing / Payment"),
        ]

        for keywords, label in rules:
            if any(k in s for k in keywords):
                return label

        return "Other"


def build_dataframe_from_sql_table(df_sql: pd.DataFrame) -> pd.DataFrame:
        """
        Convert SQL table rows into the same normalized DataFrame shape
        previously produced from JSON.
        """
        if df_sql is None or df_sql.empty:
            return pd.DataFrame()

        df_sql = df_sql.copy()

        # Map SQL columns -> app schema
        df = pd.DataFrame()

        df["id"] = df_sql.get("message_id")
        df["conversationId"] = df_sql.get("conversation_id")
        df["folder"] = df_sql.get("folder_name").fillna("").astype(str)

        df["subject"] = df_sql.get("subject").fillna("").astype(str)
        df["subject_type"] = df["subject"].apply(classify_subject)

        from_addr = df_sql.get("from_address")
        sender_addr = df_sql.get("sender_address")

        # normalize address fields
        df["from_address"] = (
            from_addr.fillna(sender_addr).fillna("").astype(str).str.lower()
            if from_addr is not None
            else sender_addr.fillna("").astype(str).str.lower()
        )
        df["sender_address"] = sender_addr.fillna("").astype(str).str.lower() if sender_addr is not None else ""

        # Recipients JSON -> list[str]
        if "to_recipients_json" in df_sql.columns:
            df["to_addresses"] = df_sql["to_recipients_json"].apply(parse_recipients_json)
        else:
            df["to_addresses"] = [[] for _ in range(len(df_sql))]

        if "cc_recipients_json" in df_sql.columns:
            df["cc_addresses"] = df_sql["cc_recipients_json"].apply(parse_recipients_json)
        else:
            df["cc_addresses"] = [[] for _ in range(len(df_sql))]

        # Datetimes -> UTC-aware
        # Prefer SQL datetime columns; if string Microsoft format appears, parse_ms_json_datetime still handles it.
        sent_src = df_sql.get("sent_datetime")
        recv_src = df_sql.get("received_datetime")

        if sent_src is not None:
            df["sent_dt"] = pd.to_datetime(sent_src, errors="coerce", utc=True)
            # handle any odd MS /Date(...)/
            mask_ms = df["sent_dt"].isna() & sent_src.astype(str).str.startswith("/Date(", na=False)
            if mask_ms.any():
                df.loc[mask_ms, "sent_dt"] = sent_src.loc[mask_ms].apply(parse_ms_json_datetime)
        else:
            df["sent_dt"] = pd.NaT

        if recv_src is not None:
            df["recv_dt"] = pd.to_datetime(recv_src, errors="coerce", utc=True)
            mask_ms = df["recv_dt"].isna() & recv_src.astype(str).str.startswith("/Date(", na=False)
            if mask_ms.any():
                df.loc[mask_ms, "recv_dt"] = recv_src.loc[mask_ms].apply(parse_ms_json_datetime)
        else:
            df["recv_dt"] = pd.NaT

        df["body_preview"] = df_sql.get("body_preview").fillna("").astype(str)

        # Derived fields
        if not df.empty:
            mailbox_lower = MAILBOX.lower()
            df["direction"] = df["from_address"].apply(
                lambda a: "Outbound" if a == mailbox_lower else "Inbound"
            )
            df["counterparty"] = df.apply(
                lambda r: (
                    (r["to_addresses"][0] if r["to_addresses"] else None)
                    if r["direction"] == "Outbound"
                    else r["from_address"]
                ),
                axis=1,
            )

        return df


def compute_response_times(df: pd.DataFrame) -> pd.DataFrame:
        """Compute response times within each conversationId."""
        if df.empty:
            return pd.DataFrame()

        df = df.dropna(subset=["conversationId", "sent_dt"]).copy()
        df = df.sort_values(["conversationId", "sent_dt"])

        records = []
        for conv_id, g in df.groupby("conversationId"):
            g = g.sort_values("sent_dt")
            prev_row = None
            for _, row in g.iterrows():
                if prev_row is not None and row["from_address"] != prev_row["from_address"]:
                    delta = row["sent_dt"] - prev_row["sent_dt"]
                    records.append(
                        {
                            "conversationId": conv_id,
                            "responder": row["from_address"],
                            "responding_to": prev_row["from_address"],
                            "response_time_hours": delta.total_seconds() / 3600,
                            "response_sent_dt": row["sent_dt"],
                        }
                    )
                prev_row = row

        return pd.DataFrame(records)


def add_time_buckets(df: pd.DataFrame) -> pd.DataFrame:
        """Add week / month / quarter derived date fields for grouping."""
        if df.empty:
            return df

        df = df.copy()
        df["date"] = df["sent_dt"].dt.tz_convert("UTC").dt.tz_localize(None).dt.date

        mask = df["sent_dt"].notna()
        base = df.loc[mask, "sent_dt"].dt.tz_convert("UTC").dt.tz_localize(None)

        df.loc[mask, "week"] = base.dt.to_period("W").dt.start_time.dt.date
        df.loc[mask, "month"] = base.dt.to_period("M").dt.start_time.dt.date
        df.loc[mask, "quarter"] = base.dt.to_period("Q").dt.start_time.dt.date

        return df


# ============================================================
# REACTIVE DATA (UPDATED: SQL TABLE SOURCE)
# ============================================================

@reactive.Calc
def raw_email_data() -> dict:
        df_sql = load_sql_messages()
        df = build_dataframe_from_sql_table(df_sql)
        if not df.empty:
            df = add_time_buckets(df)
        resp_df = compute_response_times(df)
        return {"messages": df, "responses": resp_df}


# ============================================================
# UI HELPERS
# ============================================================

def scroll_table(output_id: str, height: str = "200px"):
        """Wrap output_table in a scrollable container with sticky header."""
        return ui.div(
            ui.output_table(output_id),
            class_="table-scroll",
            style=f"height:{height}; max-height:{height}; min-height:{height};",
        )


# ============================================================
# UI - PROFESSIONAL DESIGN SYSTEM
# ============================================================

app_ui = ui.page_sidebar(
        ui.sidebar(
            ui.div(
                ui.HTML("""
            <svg width="28" height="28" viewBox="0 0 24 24" style="margin-right:10px;">
            <circle cx="12" cy="12" r="10" fill="#0066cc" opacity="0.15"/>
            <path d="M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"
                    fill="#0066cc"/>
            </svg>
            """),

                ui.tags.span("ECM Analytics", style="font-size: 1.2rem; font-weight: 700; color: #212529; letter-spacing: -0.01em;"),
                style="display: flex; align-items: center; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 2px solid #0066cc;"
            ),

            ui.h4("Data Filters"),

            ui.input_date_range(
                "date_range",
                "Date Range",
                start=(datetime.now() - pd.Timedelta(days=30)).date(),
                end=datetime.now().date(),
            ),

            ui.input_radio_buttons(
                "granularity",
                "Time Granularity",
                choices={"week": "Weekly", "month": "Monthly", "quarter": "Quarterly"},
                selected="week",
                inline=False,
            ),

            ui.input_select(
                "direction",
                "Email Direction",
                choices={
                    "both": "All Emails",
                    "Inbound": "Inbound Only",
                    "Outbound": "Outbound Only",
                },
                selected="both",
            ),

            ui.input_select(
                "subject_type",
                "Category",
                choices={"All": "All Categories"},
                selected="All",
            ),

            ui.tags.div(style="flex-grow: 1;"),

            ui.tags.div(
                ui.tags.div(
                    ui.tags.small(f"Mailbox: {MAILBOX}", style="font-size: 0.75rem; color: #6c757d;"),
                    style="margin-top: auto; padding-top: 1rem; border-top: 1px solid #dee2e6;"
                ),
            ),
        ),

        ui.tags.head(),

        ui.tags.style(
            """
            /* ========== GLOBAL FOUNDATION ========== */
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }

            html, body {
                height: 100%;
                font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Roboto', 'Helvetica Neue', Arial, sans-serif;
                background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
                color: #212529;
                -webkit-font-smoothing: antialiased;
                -moz-osx-font-smoothing: grayscale;
                font-size: 13px;
            }

            .bslib-page-sidebar {
                height: 100vh;
                display: flex;
                overflow: hidden;
            }

            .bslib-page-sidebar .main {
                padding: 0.75rem 0.5rem 0.5rem 4.0rem;
                overflow-y: hidden;
                background: transparent;
                display: flex;
                flex-direction: column;
                height: 100vh;
            }

            /* ========== SIDEBAR - CORPORATE DESIGN ========== */
            .bslib-page-sidebar .sidebar {
                background: linear-gradient(180deg, #ffffff 0%, #f8f9fa 100%);
                border-right: 2px solid #0066cc;
                box-shadow: 2px 0 12px rgba(0, 102, 204, 0.1);
                color: #212529;
                padding: 1.5rem 1.25rem;
                width: 280px;
                display: flex;
                flex-direction: column;
            }

            .bslib-page-sidebar .sidebar h4 {
                font-size: 0.75rem;
                font-weight: 700;
                letter-spacing: 0.08em;
                text-transform: uppercase;
                margin-bottom: 1.25rem;
                margin-top: 0.5rem;
                color: #495057;
                border-bottom: 2px solid #0066cc;
                padding-bottom: 0.5rem;
            }

            .bslib-page-sidebar .sidebar .form-label,
            .bslib-page-sidebar .sidebar label {
                color: #495057;
                font-size: 0.8rem;
                font-weight: 600;
                letter-spacing: 0.01em;
                margin-bottom: 0.5rem;
                display: block;
            }

            .bslib-page-sidebar .sidebar .form-control,
            .bslib-page-sidebar .sidebar .form-select {
                border-radius: 4px;
                border: 1px solid #ced4da;
                background-color: #ffffff;
                color: #212529;
                font-size: 0.85rem;
                padding: 0.6rem 0.85rem;
                transition: all 0.2s ease;
            }

            /* FIX: Dropdown menu visibility */
            .bslib-page-sidebar .sidebar .form-select option {
                background-color: #ffffff;
                color: #212529;
                padding: 0.5rem;
            }

            .bslib-page-sidebar .sidebar .form-control:focus,
            .bslib-page-sidebar .sidebar .form-select:focus {
                background-color: #ffffff;
                border-color: #0066cc;
                box-shadow: 0 0 0 3px rgba(0, 102, 204, 0.15);
                outline: none;
            }

            .bslib-page-sidebar .sidebar .shiny-input-radiogroup {
                margin-bottom: 1.25rem;
            }

            .bslib-page-sidebar .sidebar .shiny-input-radiogroup .radio {
                margin-bottom: 0.5rem;
            }

            .bslib-page-sidebar .sidebar .shiny-input-radiogroup label {
                color: #495057;
                font-weight: 400;
                font-size: 0.85rem;
            }

            /* ========== DASHBOARD HEADER - CORPORATE ========== */
            h2.dashboard-title {
                font-weight: 600;
                font-size: 1.35rem;
                color: #212529;
                margin-bottom: 0.5rem;
                letter-spacing: -0.01em;
                position: relative;
                padding-bottom: 0.4rem;
                flex-shrink: 0;
                border-bottom: 3px solid #0066cc;
                background: linear-gradient(90deg, rgba(0, 102, 204, 0.05) 0%, transparent 50%);
                padding-left: 0.5rem;
            }

            /* ========== NAVIGATION TABS - CORPORATE ========== */
            .nav-tabs {
                border-bottom: 2px solid #dee2e6;
                margin-bottom: 0.75rem;
                gap: 0.25rem;
                flex-shrink: 0;
                background: linear-gradient(90deg, rgba(0, 102, 204, 0.03) 0%, transparent 100%);
                padding-left: 0.25rem;
            }

            .nav-tabs .nav-link {
                border-radius: 0;
                padding: 0.6rem 1.25rem;
                font-size: 0.8rem;
                font-weight: 500;
                color: #6c757d;
                background-color: transparent;
                border: none;
                border-bottom: 3px solid transparent;
                transition: all 0.2s ease;
                position: relative;
            }

            .nav-tabs .nav-link:hover {
                color: #0066cc;
                background-color: #f8f9fa;
                border-bottom-color: #0066cc;
            }

            .nav-tabs .nav-link.active {
                background: linear-gradient(180deg, rgba(0, 102, 204, 0.08) 0%, transparent 100%);
                color: #0066cc;
                border-bottom: 3px solid #0066cc;
                font-weight: 600;
            }

            /* ========== TAB CONTENT - FILL REMAINING SPACE ========== */
            .tab-content {
                flex: 1 1 auto;
                overflow-y: auto;
                overflow-x: hidden;
                padding-right: 0.5rem;
            }

            .tab-pane {
                height: 100%;
            }

            /* ========== CARDS - CORPORATE DESIGN ========== */
            .card {
                border-radius: 6px;
                border: 1px solid #dee2e6;
                background: linear-gradient(135deg, #ffffff 0%, #fafbfc 100%);
                box-shadow: 0 2px 4px rgba(0, 102, 204, 0.08);
                overflow: hidden;
                transition: all 0.2s ease;
                height: 100%;
            }

            .card:hover {
                box-shadow: 0 4px 12px rgba(0, 102, 204, 0.15);
                transform: translateY(-1px);
            }

            .card-header {
                font-size: 0.8rem;
                text-transform: none;
                letter-spacing: 0;
                font-weight: 600;
                color: #0066cc;
                background: linear-gradient(135deg, #f0f7ff 0%, #f8f9fa 100%);
                border-bottom: 2px solid #0066cc;
                padding: 0.6rem 0.85rem;
            }

            .card-body {
                padding: 0.75rem;
            }

            /* ========== KPI CARDS - CORPORATE ========== */
            .kpi-card {
                color: #ffffff;
                border: none;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            }

            .kpi-card .card-body {
                padding: 0.85rem 1rem;
            }

            .kpi-label {
                font-size: 0.7rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                font-weight: 600;
                opacity: 0.95;
                margin-bottom: 0.35rem;
            }

            .kpi-card .shiny-text-output {
                display: block;
                font-size: 1.6rem;
                font-weight: 700;
                margin-top: 0.2rem;
                line-height: 1;
            }

            /* ========== LAYOUT COLUMNS ========== */
            .layout-columns {
                margin-bottom: 0.4rem;
            }

            /* ========== TABLES - CORPORATE ========== */
            .table-scroll {
                overflow-y: auto;
                overflow-x: auto;
                border-radius: 6px;
                border: 2px solid #e3f2fd;
                background: linear-gradient(135deg, #ffffff 0%, #fafbfc 100%);
                box-shadow: 0 2px 4px rgba(0, 102, 204, 0.08);
            }

            .table-scroll table {
                width: 100%;
                border-collapse: separate;
                border-spacing: 0;
                font-size: 0.75rem;
            }

            .table-scroll thead th {
                position: sticky;
                top: 0;
                z-index: 10;
                background: linear-gradient(180deg, #e3f2fd 0%, #f0f7ff 100%);
                color: #0066cc;
                font-size: 0.68rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                font-weight: 600;
                padding: 0.5rem 0.75rem;
                border-bottom: 2px solid #0066cc;
                text-align: left;
                white-space: nowrap;
            }

            .table-scroll tbody td {
                padding: 0.45rem 0.75rem;
                border-bottom: 1px solid #e3f2fd;
                color: #212529;
                vertical-align: middle;
                background: #ffffff;
            }

            .table-scroll tbody tr:nth-child(even) td {
                background: #f8fcff;
            }

            .table-scroll tbody tr:hover td {
                background: #e3f2fd;
            }

            .table-scroll::-webkit-scrollbar {
                height: 10px;
            }

            .table-scroll::-webkit-scrollbar-track {
                background: #f8f9fa;
                border-radius: 4px;
            }

            .table-scroll::-webkit-scrollbar-thumb {
                background: #adb5bd;
                border-radius: 4px;
                border: 2px solid #f8f9fa;
            }

            .table-scroll::-webkit-scrollbar-thumb:hover {
                background: #6c757d;
            }

            /* ========== PLOT CONTAINER - CORPORATE ========== */
            .shiny-plot-output {
                background: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 0.5rem;
            }

            /* ========== UTILITIES ========== */
            hr {
                border: none;
                height: 1px;
                background: #dee2e6;
                margin: 1rem 0;
            }

            /* Recent Activity Sample - Compact columns */
            #volume_examples td,
            #volume_examples th {
                font-size: 0.7rem;
                padding: 0.35rem 0.5rem;
            }

            /* Date column - compact */
            #volume_examples td:nth-child(1),
            #volume_examples th:nth-child(1) {
                max-width: 80px;
                white-space: normal;
                word-break: break-word;
                line-height: 1.2;
            }

            /* From and To columns - wrap long emails */
            #volume_examples td:nth-child(2),
            #volume_examples th:nth-child(2),
            #volume_examples td:nth-child(3),
            #volume_examples th:nth-child(3) {
                max-width: 130px;
                white-space: normal;
                word-break: break-all;
                line-height: 1.2;
            }

            /* Subject column - wrap */
            #volume_examples td:nth-child(4),
            #volume_examples th:nth-child(4) {
                max-width: 180px;
                white-space: normal;
                word-break: break-word;
                line-height: 1.2;
            }

            /* Message log subject wrapping */
            #message_log td:nth-child(6),
            #message_log th:nth-child(6) {
                text-align: left !important;
                white-space: normal;
                word-break: break-word;
                max-width: 300px;
            }

            /* Message log column widths - narrow To and CC to give Subject more space */
            #message_log td:nth-child(4),
            #message_log th:nth-child(4),
            #message_log td:nth-child(5),
            #message_log th:nth-child(5) {
                max-width: 150px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            """
        ),

        ui.h2("ECM Email Analytics", class_="dashboard-title"),

        ui.navset_tab(
            ui.nav_panel(
                "Overview",
                ui.layout_columns(
                    ui.card(
                        {"class": "kpi-card", "style": "background: linear-gradient(135deg, #0066cc 0%, #004999 100%);"},
                        ui.card_body(
                            ui.div("Total Emails", class_="kpi-label"),
                            ui.output_text("total_emails"),
                        ),
                    ),
                    ui.card(
                        {"class": "kpi-card", "style": "background: linear-gradient(135deg, #00875a 0%, #006644 100%);"},
                        ui.card_body(
                            ui.div("Inbound vs Outbound", class_="kpi-label"),
                            ui.output_text("inbound_outbound_counts"),
                        ),
                    ),
                    ui.card(
                        {"class": "kpi-card", "style": "background: linear-gradient(135deg, #5243aa 0%, #403294 100%);"},
                        ui.card_body(
                            ui.div("Avg Response Time", class_="kpi-label"),
                            ui.output_text("avg_response_time"),
                        ),
                    ),
                ),
                ui.layout_columns(
                    ui.card(
                        ui.card_header("Email Volume Trend"),
                        ui.card_body(
                            ui.output_plot("volume_plot", height="140px"),
                        ),
                    ),
                    ui.card(
                        ui.card_header("Recent Activity Sample"),
                        ui.card_body(
                            scroll_table("volume_examples", height="140px"),
                        ),
                    ),
                    col_widths=(7, 5),
                ),

                ui.layout_columns(
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.span("Email Categories", style="flex: 1;"),
                                ui.download_button("download_subject_types", "Download", style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 2in; background-color: #0066cc; color: white; border: none;"),
                                style="display: flex; align-items: center; justify-content: space-between;"
                            )
                        ),
                        ui.card_body(
                            scroll_table("top_subject_types", height="250px"),
                        ),
                    ),
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.span("Response Performance", style="flex: 1;"),
                                ui.download_button("download_response_performance", "Download", style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 2in; background-color: #0066cc; color: white; border: none;"),
                                style="display: flex; align-items: center; justify-content: space-between;"
                            )
                        ),
                        ui.card_body(
                            scroll_table("response_time_by_responder", height="250px"),
                        ),
                    ),
                    col_widths=(6, 6),
                ),
            ),

            ui.nav_panel(
                "Counterparties",
                ui.layout_columns(
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.span("All Email Counterparties", style="flex: 1;"),
                                ui.download_button("download_counterparties", "Download", style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 2in; background-color: #0066cc; color: white; border: none;"),
                                style="display: flex; align-items: center; justify-content: space-between;"
                            )
                        ),
                        ui.card_body(
                            scroll_table("top_counterparties", height="580px"),
                        ),
                    ),
                    ui.card(
                        ui.card_header(
                            ui.div(
                                ui.span("Internal Alliance Staff", style="flex: 1;"),
                                ui.download_button("download_alliance_staff", "Download", style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 2in; background-color: #0066cc; color: white; border: none;"),
                                style="display: flex; align-items: center; justify-content: space-between;"
                            )
                        ),
                        ui.card_body(
                            scroll_table("top_alliance_counterparties", height="580px"),
                        ),
                    ),
                    col_widths=(6, 6),
                ),
            ),

            ui.nav_panel(
                "Message Log",
                ui.card(
                    ui.card_header(
                        ui.div(
                            ui.span("Complete Message Audit Trail (Latest 200)", style="flex: 1;"),
                            ui.download_button("download_message_log", "Download", style="font-size: 0.75rem; padding: 0.25rem 0.5rem; margin-left: 2in; background-color: #0066cc; color: white; border: none;"),
                            style="display: flex; align-items: center; justify-content: space-between;"
                        )
                    ),
                    ui.card_body(
                        scroll_table("message_log", height="575px"),
                    ),
                ),
            ),
        ),
    )


# ============================================================
# SERVER
# ============================================================

def server(input, output, session):

        @reactive.Calc
        def filtered_data():
            data = raw_email_data()
            df = data["messages"]

            if df.empty:
                return {"messages": df, "responses": data["responses"]}

            start_date, end_date = input.date_range()
            start = pd.to_datetime(start_date).tz_localize("UTC")
            end = (pd.to_datetime(end_date) + pd.Timedelta(days=1)).tz_localize("UTC")

            mask = (df["sent_dt"] >= start) & (df["sent_dt"] < end)
            df_filt = df.loc[mask].copy()

            direction = input.direction()
            if direction != "both":
                df_filt = df_filt[df_filt["direction"] == direction]

            subject_type = input.subject_type()
            if subject_type != "All":
                df_filt = df_filt[df_filt["subject_type"] == subject_type]

            resp_df = data["responses"]
            if not resp_df.empty:
                mask_r = (resp_df["response_sent_dt"] >= start) & (resp_df["response_sent_dt"] < end)
                resp_df = resp_df.loc[mask_r].copy()

            return {"messages": df_filt, "responses": resp_df}

        @reactive.Calc
        def granularity_col():
            g = input.granularity()
            if g == "week":
                return "week"
            elif g == "month":
                return "month"
            else:
                return "quarter"

        @reactive.Effect
        def _update_subject_type_choices():
            data = raw_email_data()
            df = data["messages"]
            if df.empty:
                return
            types = sorted(df["subject_type"].dropna().unique().tolist())
            choices = {"All": "All Categories"}
            for t in types:
                choices[t] = t
            ui.update_select("subject_type", choices=choices)

        @output
        @render.text
        def total_emails():
            df = filtered_data()["messages"]
            return f"{len(df):,}" if not df.empty else "0"

        @output
        @render.text
        def inbound_outbound_counts():
            df = filtered_data()["messages"]
            if df.empty:
                return "In: 0 | Out: 0"
            counts = df["direction"].value_counts()
            inbound = int(counts.get("Inbound", 0))
            outbound = int(counts.get("Outbound", 0))
            return f"In: {inbound:,} | Out: {outbound:,}"

        @output
        @render.text
        def avg_response_time():
            resp_df = filtered_data()["responses"]
            if resp_df.empty:
                return "N/A"
            alliance_mask = resp_df["responder"].str.endswith("@thealliance.health", na=False)
            alliance_resp = resp_df.loc[alliance_mask]
            if alliance_resp.empty:
                return "N/A"
            avg_hours = alliance_resp["response_time_hours"].mean()
            return f"{avg_hours:.1f}h"

        @output
        @render.plot
        def volume_plot():
            df = filtered_data()["messages"]
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            plt.rcParams.update({
                "font.family": "sans-serif",
                "font.sans-serif": ["Segoe UI", "Arial", "Helvetica", "DejaVu Sans"],
                "font.size": 8,
                "axes.titlesize": 9,
                "axes.labelsize": 8,
                "xtick.labelsize": 7,
                "ytick.labelsize": 7,
                "legend.fontsize": 7,
            })

            fig, ax = plt.subplots(figsize=(6, 1.9))
            fig.patch.set_facecolor("#ffffff")
            ax.set_facecolor("#fafbfc")

            if df.empty:
                ax.text(0.5, 0.5, "No data available", ha="center", va="center",
                    color="#718096", fontsize=9, style="italic")
                ax.axis("off")
                fig.tight_layout()
                return fig

            col = granularity_col()

            tmp = df[[col, "direction"]].copy()
            tmp[col] = pd.to_datetime(tmp[col], errors="coerce")

            agg = tmp.groupby([col, "direction"]).size().reset_index(name="count")
            wide = agg.pivot(index=col, columns="direction", values="count").fillna(0)
            for c in ["Inbound", "Outbound"]:
                if c not in wide.columns:
                    wide[c] = 0
            wide = wide.sort_index()

            g = input.granularity()
            freq_map = {"week": "W-MON", "month": "MS", "quarter": "QS"}
            freq = freq_map.get(g, "W-MON")

            try:
                full_idx = pd.date_range(wide.index.min(), wide.index.max(), freq=freq)
                wide = wide.reindex(full_idx, fill_value=0)
            except Exception:
                pass

            inbound = wide["Inbound"].astype(int).values
            outbound = wide["Outbound"].astype(int).values
            total = inbound + outbound

            c_in = "#0066cc"
            c_out = "#00875a"
            c_total = "#212529"

            ax.fill_between(wide.index, 0, total, color="#ff9933", alpha=0.15, linewidth=0)
            ax.plot(wide.index, total, color=c_total, linewidth=2.5, label="Total", marker='o', markersize=4, zorder=3)
            ax.plot(wide.index, inbound, color=c_in, linewidth=2, label="Inbound", marker='s', markersize=3, zorder=2)
            ax.plot(wide.index, outbound, color=c_out, linewidth=2, label="Outbound", marker='^', markersize=3, zorder=2)

            ax.set_ylabel("Count", color="#495057", fontweight=500, fontsize=9)
            ax.grid(True, axis="y", linewidth=0.5, color="#dee2e6", alpha=0.7, linestyle='--')
            ax.grid(True, axis="x", linewidth=0.3, color="#dee2e6", alpha=0.4, linestyle=':')

            for spine in ['top', 'right']:
                ax.spines[spine].set_visible(False)
            for spine in ['left', 'bottom']:
                ax.spines[spine].set_color("#adb5bd")
                ax.spines[spine].set_linewidth(1.5)

            if g == "week":
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            elif g == "month":
                ax.xaxis.set_major_locator(mdates.MonthLocator())
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%b\n'%y"))
            elif g == "quarter":
                def quarter_fmt(x, pos=None):
                    dt = mdates.num2date(x)
                    quarter = (dt.month - 1) // 3 + 1
                    return f"Q{quarter}\n{dt.year}"

                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
                ax.xaxis.set_major_formatter(plt.FuncFormatter(quarter_fmt))

            ax.tick_params(axis="x", rotation=0, colors="#495057", labelsize=8)
            ax.tick_params(axis="y", colors="#495057", labelsize=8)

            ax.legend(
                frameon=True,
                facecolor="#ffffff",
                edgecolor="#dee2e6",
                loc="upper left",
                framealpha=1,
                fontsize=8,
                shadow=False,
            )

            if total.sum() > 0:
                ymax = max(1, int(total.max()))
                ax.set_ylim(0, ymax * 1.15)

            from matplotlib.ticker import MaxNLocator
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))

            fig.tight_layout(pad=0.3)
            return fig

        @output
        @render.table
        def volume_examples():
            df = filtered_data()["messages"]
            if df.empty:
                return pd.DataFrame(columns=["Date", "From", "To", "Subject"])

            df = df.copy()
            df["primary_to"] = df["to_addresses"].apply(
                lambda xs: xs[0] if isinstance(xs, list) and xs else ""
            )

            inbound = df[df["direction"] == "Inbound"].sort_values("sent_dt", ascending=False).head(5)
            outbound = df[df["direction"] == "Outbound"].sort_values("sent_dt", ascending=False).head(5)

            samples = pd.concat([inbound, outbound], axis=0)
            if samples.empty:
                return pd.DataFrame(columns=["Date", "From", "To", "Subject"])

            samples = samples[["sent_dt", "from_address", "primary_to", "subject"]].copy()

            samples["sent_dt"] = samples["sent_dt"].apply(
                lambda x: x.strftime("%m/%d/%Y") if pd.notna(x) else ""
            )

            samples["subject"] = samples["subject"].apply(
                lambda x: (x[:45] + "...") if isinstance(x, str) and len(x) > 45 else x
            )

            samples = samples.rename(
                columns={"sent_dt": "Date", "from_address": "From", "primary_to": "To", "subject": "Subject"}
            )
            return samples

        @output
        @render.table
        def top_counterparties():
            df = filtered_data()["messages"]
            if df.empty:
                return pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"])

            rows = []
            for _, row in df.iterrows():
                direction = row.get("direction", "")
                from_addr = (row.get("from_address") or "").strip().lower()
                to_list = row.get("to_addresses") or []

                if direction == "Inbound":
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 1, "Outbound": 0})
                elif direction == "Outbound":
                    if not to_list:
                        rows.append({"email": "(unknown)", "Inbound": 0, "Outbound": 1})
                    else:
                        for addr in to_list:
                            email = (addr or "").strip().lower() or "(unknown)"
                            rows.append({"email": email, "Inbound": 0, "Outbound": 1})
                else:
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 0, "Outbound": 0})

            if not rows:
                return pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"])

            counts = pd.DataFrame(rows)
            counts = counts.groupby("email")[["Inbound", "Outbound"]].sum().reset_index()
            counts["Total"] = counts["Inbound"] + counts["Outbound"]
            counts = counts.sort_values("Total", ascending=False).head(20)
            return counts[["email", "Inbound", "Outbound", "Total"]]

        @output
        @render.table
        def top_alliance_counterparties():
            df = filtered_data()["messages"]
            if df.empty:
                return pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"])

            rows = []
            for _, row in df.iterrows():
                direction = row.get("direction", "")
                from_addr = (row.get("from_address") or "").strip().lower()
                to_list = row.get("to_addresses") or []

                if direction == "Inbound":
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 1, "Outbound": 0})
                elif direction == "Outbound":
                    if not to_list:
                        rows.append({"email": "(unknown)", "Inbound": 0, "Outbound": 1})
                    else:
                        for addr in to_list:
                            email = (addr or "").strip().lower() or "(unknown)"
                            rows.append({"email": email, "Inbound": 0, "Outbound": 1})
                else:
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 0, "Outbound": 0})

            if not rows:
                return pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"])

            counts = pd.DataFrame(rows)
            counts = counts.groupby("email")[["Inbound", "Outbound"]].sum().reset_index()
            counts["Total"] = counts["Inbound"] + counts["Outbound"]

            mask = counts["email"].str.endswith("@thealliance.health", na=False)
            counts = counts.loc[mask].sort_values("Total", ascending=False)

            if counts.empty:
                return pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"])

            return counts[["email", "Inbound", "Outbound", "Total"]]

        @output
        @render.table
        def top_subject_types():
            df = filtered_data()["messages"]
            if df.empty:
                return pd.DataFrame(columns=["Category", "Count"])
            agg = (
                df.groupby("subject_type")
                .size()
                .reset_index(name="Count")
                .sort_values("Count", ascending=False)
            )
            agg = agg.rename(columns={"subject_type": "Category"})
            return agg

        @output
        @render.table
        def response_time_by_responder():
            data = filtered_data()
            df_msgs = data["messages"]
            resp_df = data["responses"]

            if df_msgs.empty:
                return pd.DataFrame(columns=["Responder", "Avg (hrs)", "Reply Counts"])

            all_responders = (
                df_msgs["from_address"]
                .fillna("(unknown)")
                .replace("", "(unknown)")
                .str.lower()
                .unique()
            )
            alliance_responders = [r for r in all_responders if r.endswith("@thealliance.health")]
            base = pd.DataFrame({"responder": alliance_responders})

            if resp_df.empty or base.empty:
                base["Reply Counts"] = 0
                base["Avg (hrs)"] = "N/A"
                base = base.sort_values("responder")
                return base[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"})

            resp_df_alliance = resp_df[resp_df["responder"].str.endswith("@thealliance.health", na=False)]

            if resp_df_alliance.empty:
                base["Reply Counts"] = 0
                base["Avg (hrs)"] = "N/A"
                base = base.sort_values("responder")
                return base[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"})

            stats = (
                resp_df_alliance.groupby("responder")["response_time_hours"]
                .agg(["count", "mean"])
                .reset_index()
                .rename(columns={"count": "Reply Counts", "mean": "avg_hrs"})
            )

            agg = base.merge(stats, on="responder", how="left")
            agg["Reply Counts"] = agg["Reply Counts"].fillna(0).astype(int)

            def _format_avg(row):
                if row["Reply Counts"] == 0 or pd.isna(row.get("avg_hrs")):
                    return "N/A"
                return f"{row['avg_hrs']:.1f}"

            agg["Avg (hrs)"] = agg.apply(_format_avg, axis=1)
            agg = agg.sort_values(by=["Reply Counts"], ascending=False)
            return agg[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"})

        @output
        @render.table
        def message_log():
            df = filtered_data()["messages"]
            if df.empty:
                return pd.DataFrame(columns=["Date/Time", "Direction", "From", "To", "CC", "Subject"])

            out = df.copy()
            out["to"] = out["to_addresses"].apply(lambda xs: ", ".join(xs) if xs else "")
            out["cc"] = out["cc_addresses"].apply(lambda xs: ", ".join(xs) if xs else "")

            out = out[["sent_dt", "direction", "from_address", "to", "cc", "subject"]].rename(
                columns={"sent_dt": "Date/Time", "direction": "Direction", "from_address": "From", "to": "To", "cc": "CC", "subject": "Subject"}
            )

            out = out.sort_values("Date/Time", ascending=False).head(200)
            return out

        @render.download(filename="email_categories.csv")
        def download_subject_types():
            df = filtered_data()["messages"]
            if df.empty:
                yield pd.DataFrame(columns=["Category", "Count"]).to_csv(index=False)
            else:
                agg = (
                    df.groupby("subject_type")
                    .size()
                    .reset_index(name="Count")
                    .sort_values("Count", ascending=False)
                )
                agg = agg.rename(columns={"subject_type": "Category"})
                yield agg.to_csv(index=False)

        @render.download(filename="response_performance.csv")
        def download_response_performance():
            data = filtered_data()
            df_msgs = data["messages"]
            resp_df = data["responses"]

            if df_msgs.empty:
                yield pd.DataFrame(columns=["Responder", "Avg (hrs)", "Reply Counts"]).to_csv(index=False)
                return

            all_responders = (
                df_msgs["from_address"]
                .fillna("(unknown)")
                .replace("", "(unknown)")
                .str.lower()
                .unique()
            )
            alliance_responders = [r for r in all_responders if r.endswith("@thealliance.health")]
            base = pd.DataFrame({"responder": alliance_responders})

            if resp_df.empty or base.empty:
                base["Reply Counts"] = 0
                base["Avg (hrs)"] = "N/A"
                base = base.sort_values("responder")
                yield base[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"}).to_csv(index=False)
                return

            resp_df_alliance = resp_df[resp_df["responder"].str.endswith("@thealliance.health", na=False)]

            if resp_df_alliance.empty:
                base["Reply Counts"] = 0
                base["Avg (hrs)"] = "N/A"
                base = base.sort_values("responder")
                yield base[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"}).to_csv(index=False)
                return

            stats = (
                resp_df_alliance.groupby("responder")["response_time_hours"]
                .agg(["count", "mean"])
                .reset_index()
                .rename(columns={"count": "Reply Counts", "mean": "avg_hrs"})
            )

            agg = base.merge(stats, on="responder", how="left")
            agg["Reply Counts"] = agg["Reply Counts"].fillna(0).astype(int)

            def _format_avg(row):
                if row["Reply Counts"] == 0 or pd.isna(row.get("avg_hrs")):
                    return "N/A"
                return f"{row['avg_hrs']:.1f}"

            agg["Avg (hrs)"] = agg.apply(_format_avg, axis=1)
            agg = agg.sort_values(by=["Reply Counts"], ascending=False)
            yield agg[["responder", "Avg (hrs)", "Reply Counts"]].rename(columns={"responder": "Responder"}).to_csv(index=False)

        @render.download(filename="all_counterparties.csv")
        def download_counterparties():
            df = filtered_data()["messages"]
            if df.empty:
                yield pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"]).to_csv(index=False)
                return

            rows = []
            for _, row in df.iterrows():
                direction = row.get("direction", "")
                from_addr = (row.get("from_address") or "").strip().lower()
                to_list = row.get("to_addresses") or []

                if direction == "Inbound":
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 1, "Outbound": 0})
                elif direction == "Outbound":
                    if not to_list:
                        rows.append({"email": "(unknown)", "Inbound": 0, "Outbound": 1})
                    else:
                        for addr in to_list:
                            email = (addr or "").strip().lower() or "(unknown)"
                            rows.append({"email": email, "Inbound": 0, "Outbound": 1})
                else:
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 0, "Outbound": 0})

            if not rows:
                yield pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"]).to_csv(index=False)
                return

            counts = pd.DataFrame(rows)
            counts = counts.groupby("email")[["Inbound", "Outbound"]].sum().reset_index()
            counts["Total"] = counts["Inbound"] + counts["Outbound"]
            counts = counts.sort_values("Total", ascending=False).head(20)
            yield counts[["email", "Inbound", "Outbound", "Total"]].to_csv(index=False)

        @render.download(filename="alliance_staff.csv")
        def download_alliance_staff():
            df = filtered_data()["messages"]
            if df.empty:
                yield pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"]).to_csv(index=False)
                return

            rows = []
            for _, row in df.iterrows():
                direction = row.get("direction", "")
                from_addr = (row.get("from_address") or "").strip().lower()
                to_list = row.get("to_addresses") or []

                if direction == "Inbound":
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 1, "Outbound": 0})
                elif direction == "Outbound":
                    if not to_list:
                        rows.append({"email": "(unknown)", "Inbound": 0, "Outbound": 1})
                    else:
                        for addr in to_list:
                            email = (addr or "").strip().lower() or "(unknown)"
                            rows.append({"email": email, "Inbound": 0, "Outbound": 1})
                else:
                    email = from_addr or "(unknown)"
                    rows.append({"email": email, "Inbound": 0, "Outbound": 0})

            if not rows:
                yield pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"]).to_csv(index=False)
                return

            counts = pd.DataFrame(rows)
            counts = counts.groupby("email")[["Inbound", "Outbound"]].sum().reset_index()
            counts["Total"] = counts["Inbound"] + counts["Outbound"]

            mask = counts["email"].str.endswith("@thealliance.health", na=False)
            counts = counts.loc[mask].sort_values("Total", ascending=False)

            if counts.empty:
                yield pd.DataFrame(columns=["email", "Inbound", "Outbound", "Total"]).to_csv(index=False)
                return

            yield counts[["email", "Inbound", "Outbound", "Total"]].to_csv(index=False)

        @render.download(filename="message_log.csv")
        def download_message_log():
            df = filtered_data()["messages"]
            if df.empty:
                yield pd.DataFrame(columns=["Date/Time", "Direction", "From", "To", "CC", "Subject"]).to_csv(index=False)
                return

            out = df.copy()
            out["to"] = out["to_addresses"].apply(lambda xs: ", ".join(xs) if xs else "")
            out["cc"] = out["cc_addresses"].apply(lambda xs: ", ".join(xs) if xs else "")

            out = out[["sent_dt", "direction", "from_address", "to", "cc", "subject"]].rename(
                columns={"sent_dt": "Date/Time", "direction": "Direction", "from_address": "From", "to": "To", "cc": "CC", "subject": "Subject"}
            )

            out = out.sort_values("Date/Time", ascending=False).head(200)
            yield out.to_csv(index=False)


app = App(app_ui, server)

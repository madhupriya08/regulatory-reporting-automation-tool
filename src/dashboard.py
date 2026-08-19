"""Streamlit dashboard for the regulatory reconciliation break report.

Run with:  streamlit run src/dashboard.py

The dashboard executes the reconciliation SQL live rather than reading the
exported CSVs. A controller looking at a break report during a close needs to
know the numbers reflect the database as it stands right now; a stale CSV that
looks authoritative is worse than no dashboard, because it invites someone to
sign off on a break that was cleared an hour ago.

Layout follows how the tie-out is actually worked:
  KPI row      - is this quarter acceptable at all?
  Trend        - is the process improving or degrading?
  Tie-out 1    - which accounts do not foot to the GL, and why?
  Tie-out 2    - which filed numbers disagree with the GL, and why?
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (  # noqa: E402
    MATERIALITY_THRESHOLD_PCT,
    SQL_DIR,
    SQLITE_DB_PATH,
)

st.set_page_config(page_title="Regulatory Reporting Reconciliation",
                   page_icon="📋", layout="wide")

# Colour by meaning, not by chart order: breaks read as a warning everywhere
# they appear, so a reader never has to consult a legend to know if a bar is
# bad news.
ROOT_CAUSE_COLOURS = {
    "LATE_FEED": "#d97706",
    "DUPLICATE_LOAD": "#dc2626",
    "TIMING_DIFFERENCE": "#2563eb",
    "MAPPING_ERROR": "#7c3aed",
    "FEED_NOT_RECEIVED": "#991b1b",
    "MISSING_FROM_EXTRACT": "#991b1b",
    "NOT_IN_GL": "#be123c",
    "UNEXPLAINED_OVERSTATEMENT": "#0f766e",
    "NONE": "#94a3b8",
}


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def run_sql(filename: str) -> pd.DataFrame:
    """Execute one reconciliation query file against SQLite.

    Cached because the three queries are re-run on every widget interaction
    and the sub-ledger rollup is the expensive part. The cache is keyed on the
    filename, so 'Refresh data' clearing it is the only way to pick up a
    rebuilt database - which is the correct trade for a close-cycle tool where
    the data changes on a schedule, not continuously.
    """
    if not SQLITE_DB_PATH.exists():
        raise FileNotFoundError(
            f"{SQLITE_DB_PATH} not found. Run `python src/build_database.py` first."
        )
    with sqlite3.connect(SQLITE_DB_PATH) as conn:
        return pd.read_sql_query((SQL_DIR / filename).read_text(), conn)


def money(value: float) -> str:
    """Compact currency for KPI tiles: 1.2M reads faster than 1,234,567.89."""
    value = float(value)
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cutoff:
            return f"${value / cutoff:,.2f}{suffix}"
    return f"${value:,.2f}"


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------
st.title("Regulatory Reporting Reconciliation")
st.caption(
    "FR Y-14Q / CCAR pre-submission tie-out  ·  "
    f"materiality threshold {MATERIALITY_THRESHOLD_PCT}% of GL balance"
)

try:
    subledger = run_sql("01_gl_vs_subledger.sql")
    extract = run_sql("02_gl_vs_regulatory_extract.sql")
    trend = run_sql("03_quarterly_break_trend.sql")
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()

# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------
with st.sidebar:
    st.header("Filters")

    quarters = sorted(subledger["quarter"].unique())
    entities = sorted(subledger["entity"].unique())

    # Default to everything selected: a reconciliation dashboard that opens
    # pre-filtered can hide a break from someone who never touches the sidebar.
    sel_quarters = st.multiselect("Reporting quarter", quarters, default=quarters)
    sel_entities = st.multiselect("Legal entity", entities, default=entities)

    st.divider()
    st.subheader("Break-only view")
    breaks_only_sub = st.toggle("GL vs sub-ledger: breaks only", value=True)
    breaks_only_ext = st.toggle("GL vs extract: breaks only", value=True)

    st.divider()
    if st.button("Refresh data", width='stretch'):
        st.cache_data.clear()
        st.rerun()

    st.caption("Backend: SQLite (local)")


def apply_filters(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[
        frame["quarter"].isin(sel_quarters) & frame["entity"].isin(sel_entities)
    ]


sub_f = apply_filters(subledger)
ext_f = apply_filters(extract)
trend_f = trend[trend["quarter"].isin(sel_quarters)]

if sub_f.empty and ext_f.empty:
    st.warning("No accounts match the current filters.")
    st.stop()

# --------------------------------------------------------------------------
# KPI row
# --------------------------------------------------------------------------
sub_breaks = sub_f[sub_f["status"] == "BREAK"]
ext_breaks = ext_f[ext_f["status"] == "BREAK"]

# An account broken in both tie-outs is still one broken account, so the
# affected population is the DISTINCT union - the same rule query 3 applies.
broken_accounts = set(
    map(tuple, sub_breaks[["entity", "product", "quarter"]].to_numpy())
) | set(map(tuple, ext_breaks[["entity", "product", "quarter"]].to_numpy()))
population = len(sub_f)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Accounts in scope", f"{population:,}")
k2.metric("Accounts with breaks", f"{len(broken_accounts):,}")
k3.metric(
    "Break rate",
    f"{(100.0 * len(broken_accounts) / population if population else 0):.1f}%",
)
k4.metric(
    "Total break amount",
    money(sub_breaks["variance"].abs().sum() + ext_breaks["variance"].abs().sum()),
    help="Absolute variance across both tie-outs. Under- and overstatements "
         "are two problems, not zero, so they are not netted.",
)
k5.metric("Largest single break", money(
    max(
        [abs(v) for v in sub_breaks["variance"]] +
        [abs(v) for v in ext_breaks["variance"]] + [0]
    )
))

st.divider()

# --------------------------------------------------------------------------
# Quarterly trend
# --------------------------------------------------------------------------
st.subheader("Quarterly break trend")
st.caption(
    "Both measures are shown because either alone misleads: the rate catches a "
    "process degrading across many small accounts, the dollar amount catches one "
    "break on the mortgage book that outweighs twenty on credit cards."
)

t1, t2 = st.columns(2)
if trend_f.empty:
    t1.info("No trend data for the selected quarters.")
else:
    fig = px.bar(trend_f, x="quarter", y="break_rate_pct",
                 text="break_rate_pct", title="% of accounts with breaks")
    fig.update_traces(texttemplate="%{text:.1f}%", marker_color="#dc2626")
    fig.update_layout(yaxis_title="Break rate (%)", xaxis_title="", height=340)
    t1.plotly_chart(fig, width='stretch')

    stacked = trend_f.melt(
        id_vars="quarter",
        value_vars=["subledger_break_amount", "extract_break_amount"],
        var_name="tie_out", value_name="amount",
    ).replace({
        "subledger_break_amount": "GL vs sub-ledger",
        "extract_break_amount": "GL vs extract",
    })
    fig = px.bar(stacked, x="quarter", y="amount", color="tie_out",
                 title="Total break amount ($)",
                 color_discrete_map={"GL vs sub-ledger": "#d97706",
                                     "GL vs extract": "#2563eb"})
    fig.update_layout(yaxis_title="Break amount ($)", xaxis_title="",
                      legend_title="", height=340)
    t2.plotly_chart(fig, width='stretch')

    st.dataframe(trend_f, width='stretch', hide_index=True)

st.divider()

# --------------------------------------------------------------------------
# Tie-out 1: GL vs sub-ledger
# --------------------------------------------------------------------------
st.subheader("1 · General ledger vs sub-ledger")
st.caption(
    "Does the loan-level detail foot to the certified GL balance? Sub-ledger "
    "UNDER the GL means detail is missing (late feed); OVER means detail is "
    "doubled (duplicate load)."
)

c1, c2 = st.columns([1, 1])

if sub_breaks.empty:
    c1.success("No sub-ledger breaks above the materiality threshold.")
else:
    counts = sub_breaks["root_cause"].value_counts().reset_index()
    counts.columns = ["root_cause", "breaks"]
    fig = px.bar(counts, x="root_cause", y="breaks", color="root_cause",
                 title="Breaks by root cause",
                 color_discrete_map=ROOT_CAUSE_COLOURS)
    fig.update_layout(showlegend=False, xaxis_title="", yaxis_title="Breaks",
                      height=340)
    c1.plotly_chart(fig, width='stretch')

    # Signed variance %, with the materiality band drawn in: the point of the
    # chart is which bars escape the band, not their absolute height.
    fig = px.bar(
        sub_breaks.sort_values("variance_pct"),
        x="variance_pct", y="product", color="root_cause", orientation="h",
        hover_data=["entity", "quarter", "variance"],
        title="Variance vs GL (%)", color_discrete_map=ROOT_CAUSE_COLOURS,
    )
    fig.add_vline(x=MATERIALITY_THRESHOLD_PCT, line_dash="dot", line_color="#64748b")
    fig.add_vline(x=-MATERIALITY_THRESHOLD_PCT, line_dash="dot", line_color="#64748b")
    fig.update_layout(xaxis_title="Variance (% of GL)", yaxis_title="",
                      legend_title="", height=340)
    c2.plotly_chart(fig, width='stretch')

st.dataframe(
    (sub_breaks if breaks_only_sub else sub_f).reset_index(drop=True),
    width='stretch', hide_index=True,
)

st.divider()

# --------------------------------------------------------------------------
# Tie-out 2: GL vs regulatory extract
# --------------------------------------------------------------------------
st.subheader("2 · General ledger vs regulatory extract")
st.caption(
    "Does the number that reached the regulator match the GL? A variance with "
    "an equal and opposite partner in the same entity and quarter is a mapping "
    "error (balance re-tagged to the wrong product); a one-sided variance is a "
    "timing difference (GL adjustment posted after the extract ran)."
)

c1, c2 = st.columns([1, 1])

if ext_breaks.empty:
    c1.success("No extract breaks above the materiality threshold.")
else:
    counts = ext_breaks["root_cause"].value_counts().reset_index()
    counts.columns = ["root_cause", "breaks"]
    fig = px.bar(counts, x="root_cause", y="breaks", color="root_cause",
                 title="Breaks by root cause",
                 color_discrete_map=ROOT_CAUSE_COLOURS)
    fig.update_layout(showlegend=False, xaxis_title="", yaxis_title="Breaks",
                      height=340)
    c1.plotly_chart(fig, width='stretch')

    fig = px.bar(
        ext_breaks.sort_values("variance"),
        x="variance", y="product", color="root_cause", orientation="h",
        hover_data=["entity", "quarter", "variance_pct", "offsetting_product"],
        title="Variance vs GL ($)", color_discrete_map=ROOT_CAUSE_COLOURS,
    )
    # Mapping-error pairs are visible here as two bars of equal length on
    # opposite sides of zero - the signature the SQL keys on.
    fig.add_vline(x=0, line_color="#334155")
    fig.update_layout(xaxis_title="Variance ($)", yaxis_title="",
                      legend_title="", height=340)
    c2.plotly_chart(fig, width='stretch')

st.dataframe(
    (ext_breaks if breaks_only_ext else ext_f).reset_index(drop=True),
    width='stretch', hide_index=True,
)

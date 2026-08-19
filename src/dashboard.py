"""Streamlit dashboard for the regulatory reconciliation break report.

Run with:  streamlit run src/dashboard.py                 (SQLite, the default)
           USE_SNOWFLAKE=true streamlit run src/dashboard.py   (live Snowflake)

BACKEND AUTO-DETECTION
The engine is chosen by the USE_SNOWFLAKE environment variable and by nothing
else in this file. Every query goes through src/backends.py, which resolves the
right SQL file for the active engine and lowercases the result columns, so
there is not one branch on the backend anywhere below this docstring. Switching
between local SQLite and a live warehouse takes an environment variable and no
code change - which is the property that makes it safe to demo locally and run
for real without maintaining two versions of the dashboard that drift.

The casing detail matters more than it sounds: Snowflake returns ENTITY where
SQLite returns entity, so every `frame["entity"]` below would raise KeyError on
one of the two backends. backends.normalize_columns fixes it once at the
boundary rather than scattering case handling through the UI code.

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

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MATERIALITY_THRESHOLD_PCT  # noqa: E402
from src.backends import backend_name, resolve_query_path, run_reconciliation  # noqa: E402

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
@st.cache_data(show_spinner="Running reconciliation...")
def run_recon(stem: str, backend: str) -> pd.DataFrame:
    """Execute one reconciliation on the active backend.

    Caching matters more here than it did on SQLite alone: on Snowflake every
    uncached call is a warehouse round trip that costs real credits, and a
    dashboard that re-queried on every checkbox toggle would be expensive as
    well as slow.

    `backend` is an argument purely so it forms part of the cache key. It is
    never read in the body. Without it, flipping USE_SNOWFLAKE and restarting
    would serve results the other engine produced - a silently wrong number on
    a regulatory report, which is the worst kind of caching bug. Passing it
    makes the two backends occupy separate cache entries by construction.
    """
    return run_reconciliation(stem)


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

BACKEND = backend_name()

try:
    subledger = run_recon("01_gl_vs_subledger", BACKEND)
    extract = run_recon("02_gl_vs_regulatory_extract", BACKEND)
    trend = run_recon("03_quarterly_break_trend", BACKEND)
except FileNotFoundError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:  # noqa: BLE001
    # SnowflakeConfigError lands here when USE_SNOWFLAKE is set but the
    # credentials are not. Its message already names exactly which variables
    # are missing, so it is rendered as-is: a raw traceback in the browser
    # would bury that list and tells the operator nothing they can act on.
    st.error(f"Could not run the reconciliation on {BACKEND}.")
    st.code(str(exc))
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

    st.divider()
    st.subheader("Backend")
    # Shown, not hidden: anyone reading a break report needs to know whether
    # they are looking at a local sample or the live warehouse before they act
    # on it, and which SQL file produced each number.
    if BACKEND == "Snowflake":
        st.success("Snowflake (live)")
    else:
        st.info("SQLite (local)")
    st.caption("Set USE_SNOWFLAKE=true to run against Snowflake.")
    with st.expander("SQL in use"):
        for stem in ("01_gl_vs_subledger", "02_gl_vs_regulatory_extract",
                     "03_quarterly_break_trend"):
            st.caption(
                f"`{resolve_query_path(stem).relative_to(Path(__file__).resolve().parents[1])}`"
            )


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

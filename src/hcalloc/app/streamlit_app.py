"""Offline planning app. Run with:  hcalloc app   (or: streamlit run src/hcalloc/app/streamlit_app.py)

Runs entirely on the local machine. Data is a tidy CSV of daily aggregates
(date, unit, census, admissions, discharges) or the built-in synthetic demo.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from hcalloc.config import PlanSettings, default_hospital
from hcalloc.data.series import DailySeries
from hcalloc.data.synthetic import simulate_hospital
from hcalloc.service import FORECASTERS, REASON_CODES, DecisionLog, plan_next_week, recalibrate

st.set_page_config(page_title="Hospital resource planner", layout="wide")
hospital = default_hospital()


@st.cache_data
def demo_series() -> pd.DataFrame:
    return simulate_hospital(seed=0, years=3).to_frame()


with st.sidebar:
    st.header("Data")
    upload = st.file_uploader("Daily aggregates CSV (date, unit, census, admissions, discharges)", type="csv")
    if upload is not None:
        series = DailySeries.from_frame(pd.read_csv(upload), units=hospital.unit_names)
        st.caption("Using uploaded data.")
    else:
        series = DailySeries.from_frame(demo_series(), units=hospital.unit_names)
        st.warning("Demo mode: SYNTHETIC data, not a real hospital.")
    origin = st.date_input("Plan the week after", value=series.dates[-1].date(),
                           min_value=series.dates[400].date(), max_value=series.dates[-1].date())
    st.header("Model")
    forecaster = st.selectbox("Forecaster", list(FORECASTERS), index=0)
    risk = st.slider("Risk aversion (weight on worst 10% of outcomes)", 0.0, 1.0, 0.3, 0.1)
    mult = st.number_input("Shortage cost multiplier", 0.5, 5.0, 1.0, 0.1,
                           help="Raise to make the plan more cautious. Override learning can suggest a value.")
    n_scen = st.select_slider("Scenarios", options=[20, 40, 60], value=40)
    run = st.button("Make plan", type="primary")

st.title("Weekly bed, nurse and pump plan")

if run:
    with st.spinner("Forecasting and optimising ..."):
        st.session_state.result = plan_next_week(
            series, origin=pd.Timestamp(origin), forecaster=forecaster,
            settings=PlanSettings(risk_weight=risk, shortage_multiplier=mult), n_scenarios=n_scen,
        )

result = st.session_state.get("result")
if result is None:
    st.info("Choose settings in the sidebar and press **Make plan**.")
    st.stop()

plan = result.plan
c1, c2, c3 = st.columns(3)
c1.metric("Planned cost (first stage)", f"{plan.first_stage_cost:,.0f}")
c2.metric("Expected extra cost (recourse)", f"{plan.expected_recourse:,.0f}")
c3.metric("Bad-week extra cost (CVaR 90%)", f"{plan.cvar(0.9):,.0f}")

st.subheader("Forecast census with 80% band, and beds opened")
lo, hi = result.band(0.8)
tabs = st.tabs(result.units)
for j, tab in enumerate(tabs):
    with tab:
        df = pd.DataFrame({
            "median": np.median(result.scenarios[:, j], axis=0),
            "p10": lo[j], "p90": hi[j], "beds opened": plan.beds[j],
        }, index=result.days.date)
        st.line_chart(df)

st.subheader("Recommendation — edit any figure to override")
table = result.table()
edited = st.data_editor(
    table, disabled=["day", "unit", "forecast_median", "forecast_p10", "forecast_p90"],
    hide_index=True, use_container_width=True, key="editor",
)
reason = st.selectbox("Reason for changes (if any)", ("",) + REASON_CODES)
note = st.text_input("Note (optional, no patient details)")
log = DecisionLog()

if st.button("Save final decision"):
    units, days = result.units, list(result.days.date)
    final = {}
    for k in ("beds", "nurses", "pumps"):
        final[k] = edited.pivot(index="unit", columns="day", values=k).reindex(index=units, columns=days).to_numpy().astype(int)
    changed = any((final[k] != result.plan.first_stage()[k]).any() for k in final)
    if changed and not reason:
        st.error("Please pick a reason for the override.")
    else:
        rid = log.record(result, final, reason=reason, note=note)
        st.success(f"Saved decision #{rid}" + (" (override)" if changed else " (accepted as recommended)"))

with st.expander("Learn from past overrides"):
    rows = log.load()
    st.write(f"{len(rows)} saved decisions, {sum(any((r['final'][k] != r['recommended'][k]).any() for k in ('beds', 'nurses')) for r in rows)} with overrides.")
    if st.button("Suggest cost weights from overrides", disabled=len(rows) < 2):
        with st.spinner("Re-solving past weeks under candidate weights ..."):
            theta, learner = recalibrate(log, hospital, PlanSettings(risk_weight=risk, shortage_multiplier=mult))
        st.write(f"Suggested shortage multiplier **{theta[0]}**, risk weight **{theta[1]}**. "
                 "Review before applying: set them in the sidebar if you agree.")
        st.dataframe(pd.DataFrame([{"multiplier": t[0], "risk_weight": t[1], "mismatch": s} for t, s in learner.scores.items()])
                     .sort_values("mismatch"), hide_index=True)

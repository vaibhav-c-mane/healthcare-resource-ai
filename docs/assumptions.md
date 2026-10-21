# Assumptions

Every value here is an assumption chosen to make the default hospital plausible. None is measured from a real hospital. Replace them with local figures before using any result for decisions, and re-run the cost sensitivity checks.

## Cost units

Costs are relative. **1.0 = the cost of one rostered nurse-day.**

| Parameter | Value | Where |
| --- | --- | --- |
| Rostered nurse-day | 1.0 | `HospitalConfig.nurse_cost` |
| Agency / overtime nurse-day | 2.0 | `HospitalConfig.agency_cost` |
| Redeploying a rostered nurse to another unit | 0.15 per move | `HospitalConfig.nurse_move_cost` |
| Moving one infusion pump between units | 0.1 per move | `HospitalConfig.pump_move_cost` |
| Opened bed-day (cleaning, support staff) | 0.3 | `Unit.bed_cost` |
| Escalation (surge) bed-day | 1.5 | `Unit.surge_cost` |
| Unserved patient-day, ICU | 12 | `Unit.shortage_cost` |
| Unserved patient-day, other units | 6 | `Unit.shortage_cost` |

"Unserved" stands for boarding in the ED, diversion, outlying, or a cancelled elective. The shortage-to-nurse cost ratio is the assumption results are most sensitive to; the plan calls for a sweep from 2:1 to 20:1.

## Default hospital (`config.default_hospital`)

| Unit | Beds | Surge beds | Patients per nurse (hard limit) | Pumps per patient | Agency nurses max/day | Float pool allowed |
| --- | --- | --- | --- | --- | --- | --- |
| ICU | 20 | 4 | 2 | 3.0 | 3 | no |
| StepDown | 24 | 4 | 3 | 1.5 | 4 | yes |
| Medical | 76 | 10 | 5 | 0.5 | 4 | yes |
| Surgical | 48 | 6 | 5 | 0.8 | 4 | yes |

- Rostered nurses available per day across all units: 48. Float pool: 4 per day. Pump fleet: 150. Pump transfers per day: at most 20.
- Allowed nurse redeployments: ICU to any unit; StepDown to Medical or Surgical; Medical and Surgical to each other. Ward nurses never move into ICU.
- A "nurse" is one nurse position for the day. Ratios are applied per day, not per shift.

## Model simplifications

- Days in the horizon are linked only through first-stage decisions. Unserved patients do not carry over to the next day.
- Patients cannot overflow into another unit's beds. In real hospitals outlying is common, so shortage here is an upper bound.
- Recourse decisions (agency, moves, surge beds) are continuous; first-stage decisions are integers.
- Pumps start each day fully deployed across units; there is no central store.
- The plan is made once a week (Sunday for Monday to Sunday). Daily re-planning is supported by the code but not used in the benchmarks.
- MIP solves stop at a 0.5% optimality gap.

## Synthetic demand (`data/synthetic.py`)

| Unit | Admissions/day | Weekday pattern | Winter peak | Respiratory waves | Mean LOS (days) | Weekend discharge delay |
| --- | --- | --- | --- | --- | --- | --- |
| ICU | 4.0 | almost flat | +10% | full | 3.5 | 20% |
| StepDown | 6.0 | slightly lower at weekends | +8% | 60% | 3.0 | 40% |
| Medical | 11.0 | Monday high, Saturday low | +15% | full | 5.0 | 50% |
| Surgical | 9.5 | electives Mon to Fri, weekends at about 30% | -3% | electives cut 30% | 4.0 | 30% |

- Respiratory waves: one per winter, peak between 1 December and mid-February, amplitude 5% to 35%, width 10 to 25 days.
- Day-to-day noise: gamma multiplier with coefficient of variation about 16%, then Poisson arrivals.
- Length of stay is lognormal; discharges due on a Saturday or Sunday slip to Monday with the probability above.
- Holidays follow a UK-style calendar; electives fall to 40% on holidays.
- Trend: +1% demand per year.
- Stress wave (robustness test): a 70-day bump from 1 October 2023. At its peak, admissions rise 50% in ICU, 30% in StepDown and 40% in Medical, and fall 25% in Surgical.

## Simulated administrator (`overrides/admin_sim.py`)

- Plans with a shortage-cost multiplier of 1.8 and risk weight 0.5, which the system is not told. Both are deliberately off the learner's grid.
- Changes 5% of bed and nurse figures by one, at random, as habitual noise.

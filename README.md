# OCP Alarm Intelligence

An alarm-analysis / root-cause platform for industrial alarm systems, built on
ISA-18.2 and EEMUA-191 methods. It started as a skeleton fed by a synthetic
generator; it now runs on a **real historian alarm log** — 102,319 alarm
transactions from an IBMS, 1,604 alarm sources, Oct 2015 → Jun 2024.

The synthetic source is still here and still works. That is the point of the
`DataSource` seam: real and simulated data enter through the same door.

## Quick start

```bash
pip install -r requirements.txt

# --- real data ---
python scripts/ingest_alarm_log.py data/preprocessed_trendedpointalarm.csv
python scripts/run_analysis.py          # text report
uvicorn src.api.main:app --reload       # API + dashboard on :8000

# --- synthetic data (no plant data needed) ---
python scripts/generate_demo_data.py
python scripts/run_analysis.py
```

`python scripts/dump_sample_data.py` refreshes the dashboard's offline fallback
(it writes `frontend/sample_data.json` *and* re-embeds it in `index.html`, so the
dashboard opens standalone with a `SAMPLE DATA` badge).

Plant data and the DuckDB file are gitignored — the store rebuilds from the CSV
in one command, and neither belongs in a public repo.

## What the real data says

The point of the exercise. All of it falls out of Layer 3 in ~12 s:

| Finding | Number |
| --- | --- |
| One asset (`AAA-BMS-SSIF`, "device offline") | **48.8%** of all alarms |
| Priority mix — high + critical | **64.8%** (ISA-18.2 target ≈ 5%) |
| Worst 10-minute window | **472 alarms** (EEMUA "manageable peak" = 10) |
| Median time to clear | **183 min**; 20% still active after 24 h |
| Standing alarms — raised, never cleared | **1,254**, the oldest standing 8+ years |
| Chattering | `T01-BMS-SSIF:OFFLINE` re-alarms within 60 s on 49% of its activations |
| Incidents segmented from the stream | **2,770** |

The average alarm rate (0.22 per 10 min) is *inside* the EEMUA target — and it is
the most misleading number in the file. The load isn't spread out; it's
concentrated in 201 flood windows and one broken point. Ranked, not averaged, is
the only way to read this system.

## How the layers map to the code

| Layer | Folder | What's there |
| --- | --- | --- |
| 1 Acquisition | `src/acquisition` | `DataSource` interface · `AlarmLogDataSource` (real CSV) · `SyntheticDataSource` · PI/OPC-UA stubs |
| 2 Preparation | `src/preparation`, `src/storage` | episode reconstruction from transitions · resample/align/outlier flags · DuckDB store (+ TimescaleDB schema) |
| 3 Analysis | `src/analysis` | bad actors · chattering · floods · co-occurrence · sequences · EEMUA-191 KPIs |
| 4 Root cause | `src/rootcause` | incident segmentation · explainable candidate ranking · accuracy check vs ground truth |
| 5 AI | — | extension point (anomaly detection, prediction) — now that the data is real |
| 6 Dashboard | `src/api`, `frontend` | FastAPI JSON + single-file React HMI console |

## What the real data forced us to change

Worth reading before extending anything — each one is a place where "works on the
demo" and "works on the plant" diverged.

**Transitions are not occurrences.** The log records `N2A` / `A2A` / `A2N`
transitions, and it is *unbalanced*: 91k returns-to-normal against 4.5k raises.
Counting rows double-counts; counting only raises throws away 89% of the history.
`src/preparation/events.py` rebuilds episodes — a raise, and the clear that closes
it — synthesising the raise where the log lost it and marking that episode
`implicit` so it is counted but never *timed*. Time-to-clear uses only the 8,135
episodes where both ends are real.

**A BMS has no LO/LOLO/HI/HIHI ladder.** `level` now carries an alarm-type code
(`OFFLINE`, `SPACE_TEMP`, `TEMP_HIGH`, `GAS` …) derived from the alarm text: 85
free-text messages collapse to 17 stable codes. The analytics only ever used
`level` as "which kind of alarm on this tag", which is exactly what that is.

**Priority is four-tier, and this site's mix is broken.** `MEDIUM` runs through the
engine, API and dashboard now. The loader trusts the site's own severity
assignment rather than quietly correcting it — 64.8% high+critical is a *finding*,
and the scorecard reports it against the ISA-18.2 target.

**The analytics were quadratic.** `chattering` was O(n²) per tag; on the 49k-alarm
bad actor that's 2.4 billion comparisons. It's vectorised with `searchsorted` now.
`co_occurrence` grows with the square of the flood size, so its forward scan is
capped — otherwise the top "correlated pairs" are an artefact of the biggest flood
rather than a relationship between two tags.

**Real data has no `incident_id`.** Layer 4 used to be handed its incidents by the
generator. It now finds them: gap-split the stream, keep bursts with enough alarms,
enough distinct sources, and at least one significant alarm.

**"Earliest wins" is not good enough for ranking.** Timestamps are minute-resolution,
so dozens of tags tie for first. Ranking now blends order with an **initiation
rate** measured across the corpus — how often does this tag *open* an incident it
appears in? A tag that appears in 638 incidents and starts 81% of them behaves like
a cause; one that appears in 160 and starts 8% is a follower. Rates are shrunk
toward the corpus base rate, so "opened the only incident it ever appeared in"
doesn't buy 100% confidence. Every candidate carries an explanation string, so an
engineer can audit the reasoning without trusting the model.

## The key design decision (unchanged — and it held)

`src/acquisition/base.py` defines `DataSource` with two methods:
`process_samples()` and `alarm_events()`. Swapping the synthetic source for the
real log was one import in one script. Everything above Layer 1 needed *scale* and
*semantics* work — but no rewiring. The seam did its job.

`AlarmLogDataSource.process_samples()` returns an empty list, and that's honest: an
alarm log carries no continuous process values. When a PI / OPC-UA feed lands, it
fills that stream and Layer 2's `align_tags` starts doing work.

## Suggested build order from here

1. **Rationalise the bad actor.** One point is half the alarm system. Nothing else
   in this repo will buy as much operator relief as fixing it.
2. **Knowledge graph (Neo4j).** `cause_hints` is a rules table standing in for it;
   the confirm-to-KB flow (`src/persistance/knowledge_base.py`) already has the
   schema for operator-confirmed causes.
3. **Causal discovery.** The initiation-rate heuristic is the ceiling of what
   ordering alone can tell you. Granger / transfer entropy needs the continuous
   historian feed — which is the argument for connecting one.
4. **Then** Layer 5 models, trained on operator-confirmed cases rather than on the
   heuristics' own output.

## What's real vs. what's demo

The analytics and KPIs are the ISA-18.2 / EEMUA-191 methods you'd ship, and they're
now measured against a real 9-year alarm history. The root-cause accuracy of 1.0
that the *synthetic* path still reports is a check on the plumbing, not a claim
about the plant: it reflects clean designed data where the causes were planted.
Real incidents have no truth label until an operator confirms one — that's what the
knowledge base is for, and why confidence sits next to every candidate instead of a
single headline accuracy number.

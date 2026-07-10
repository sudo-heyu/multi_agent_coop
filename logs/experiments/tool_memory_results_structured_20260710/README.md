# Tool-memory experiment structured results

Source directory: `/Users/heyu/Developer/ap/multi_agent_coop/logs/experiments/tool_memory_results_final_20260710`
Valid completed runs used for statistics: 80

Directory layout:
- `data/<profile>/memory_<off|on>/`: per-condition CSV rows, raw stdout/stderr logs, and SQLite event DB.
- `stats/`: statistical tables with mean/std/SEM/bootstrap 95% CI and memory-on/off comparisons.
- `figures/`: all comparison figures. The dashboard compares the 8 profile-memory conditions across the three primary metrics.

Primary metrics:
- `wall_duration_s`: wall-clock negotiation duration; lower is better.
- `effect_score`: QoS acceptance/effect score; higher is better.
- `success_rate`: fraction of completed runs whose negotiation outcome was success; higher is better.

Condition summary:

| profile | memory | n_valid | duration mean | effect mean | success rate |
|---|---:|---:|---:|---:|---:|
| none | off | 10 | 152.78 | -0.054 | 0.30 |
| none | on | 10 | 51.62 | 0.225 | 0.80 |
| basic | off | 10 | 161.56 | 0.058 | 0.70 |
| basic | on | 10 | 62.99 | 0.126 | 0.70 |
| full | off | 10 | 126.41 | 0.112 | 0.70 |
| full | on | 10 | 41.86 | 0.187 | 0.90 |
| faulty | off | 10 | 108.02 | 0.026 | 0.70 |
| faulty | on | 10 | 40.76 | 0.184 | 0.80 |

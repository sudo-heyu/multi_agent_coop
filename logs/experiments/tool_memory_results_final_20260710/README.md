# Tool-Memory Experiment Results

本目录为最终实验结果归档，来源目录：

`logs/experiments/final_tool_memory_highload_fastvote_qospos_count5_10rep`

## 数据文件

- `results.csv`: 逐次实验原始结果，包含 profile、memory、trial、outcome、turns、duration、QoS score 等字段。
- `summary.json`: 按条件聚合后的统计结果。
- `condition_metrics.csv`: 每个 profile/memory 条件的核心指标表。
- `memory_gap_metrics.csv`: memory-on 相对 memory-off 的效果增益、耗时下降、成功率提升。
- `config.json`: 本次批量实验启动参数。
- `db/`: 每个条件对应的 SQLite event/memory 数据库。
- `raw/`: 每次 trial 的 stdout/stderr 原始日志。

## 图像文件

- `summary_bars.png`: 平均轮次、平均 QoS score、平均 wall-clock duration 对比图。
- `memory_advantage.png`: memory advantage 折线图。
- `memory_gap_subplots.png`: QoS score gain、duration reduction、success-rate gain 分图。

## 核心结论

所有工具档位下，memory-on 的平均 QoS 效果和平均真实协商耗时均优于 memory-off。

| profile | score gain | duration reduction |
| --- | ---: | ---: |
| none | +0.279532 | 101.16s |
| basic | +0.067921 | 98.57s |
| full | +0.075236 | 84.54s |
| faulty | +0.157666 | 67.26s |


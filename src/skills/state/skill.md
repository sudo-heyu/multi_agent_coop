# Skill: state

## 作用

从运行在 DGX Spark 的全局状态服务器获取三台 AP 的最新采集指标，
供 orchestrator 在协商开始前读取，替代硬编码 mock 数据。

## 使用方式

```python
from src.skills.state import get_all_states, get_state, StateStaleError

# 获取全部三台 AP 的状态（返回格式与 AP_STATE mock 兼容）
ap_state = get_all_states()                         # 默认服务器 localhost:5001
ap_state = get_all_states("http://192.168.1.100:5001")

# 获取单台 AP 的状态
ap1_state = get_state("ap1")
```

## 错误处理

| 异常 | 原因 |
|---|---|
| `ConnectionError` | 状态服务器不可达 |
| `StateStaleError` | 某台 AP 数据缺失或超过 60 秒未更新 |

## 目录说明

```
state/
├── skill.md          # 本文件，Skill 介绍
├── scripts/
│   └── state.py      # get_all_states() / get_state() 实现
└── reference/
    └── metrics.md    # 所有状态字段的含义与作用
```

## 数据新鲜度

服务器对每台 AP 的数据标记 `age_seconds` 和 `stale` 字段。
`stale: true` 表示超过 60 秒未收到上报，此时 `get_all_states()` 抛出 `StateStaleError`，
协商不会在数据过期的情况下启动。

# 模型运行时与早期 Ollama 实测记录

> **现行架构说明**：项目由 OpenClaw 托管 AP agent，模型不再由本项目直连
> Ollama，而是经 OpenClaw 的 provider 机制调用；默认必须使用 PPIO API
> `qwen80binstruct`（当前 alias 指向 PPIO 可用的后继模型），无 PPIO key 时 setup 失败，
> 不再自动回退本地 Ollama。本文保留为**早期直连 Ollama 运行时的实测记录**，其中的
> `/api/chat`、`think`、模型选型与耗时数据仍可作为本地模型行为的参考。

以下内容均为历史实测，不代表当前默认 provider 配置。历史测试中的本地模型通过 Ollama 运行：

- 服务地址：`http://localhost:11434`
- 模型：`qwen3.6:27b`
- 可选轻量模型：`qwen3:14b`
- 默认调用：流式输出，`"stream": true`

流式输出不会减少模型总生成耗时，但可以让前端在生成过程中持续收到 token，适合 dashboard 展示 agent 对话过程。

## 禁用 thinking（默认）

项目默认禁用 thinking。必须使用 Ollama 原生 `/api/chat` 接口，并在请求体顶层加入 `"think": false`。

```bash
curl --no-buffer http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b",
    "think": false,
    "stream": true,
    "messages": [
      {
        "role": "user",
        "content": "AP1的TX Power是16dBm，邻居AP2收到的RSSI是-65dBm，CCA阈值-82dBm，余量3dBm，请计算路径损耗和目标TX Power。请简洁回答。"
      }
    ]
  }'
```

本次流式测试结果：

- 用时：`28.67s`（`total_duration=28669812565ns`）
- 结束原因：`stop`
- 生成 token 数：`209`
- 流式 chunk 数：`187`
- 返回情况：`message.content` 分块返回，没有 `thinking` 字段

拼接后的真实返回内容：

```text
根据自由空间损耗公式和给定参数，计算如下：

**1. 路径损耗 (Path Loss, PL)**
PL = TX Power - RSSI = 16 - (-65) = 81 dBm

**2. 目标 TX Power**
通常“目标 TX Power”指满足最小接收灵敏度（基于 CCA 阈值 + 余量）所需的发射功率。

所需最小接收功率 = CCA阈值 + 余量 = -82 + 3 = -79 dBm

目标 TX Power = 所需最小接收功率 + 路径损耗 = -79 + 81 = 2 dBm

结论：
- 路径损耗：81 dB
- 目标 TX Power：2 dBm
```

最后一个流式 chunk 的关键字段：

```json
{
  "done": true,
  "done_reason": "stop",
  "total_duration": 28669812565,
  "load_duration": 7460914895,
  "prompt_eval_count": 62,
  "prompt_eval_duration": 186348366,
  "eval_count": 209,
  "eval_duration": 20904064885
}
```

## 启用 thinking

启用 thinking 时，仍使用 `/api/chat`，但不传 `"think": false`。

```bash
curl --no-buffer http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b",
    "stream": true,
    "options": {
      "num_predict": 1024
    },
    "messages": [
      {
        "role": "user",
        "content": "AP1的TX Power是16dBm，邻居AP2收到的RSSI是-65dBm，CCA阈值-82dBm，余量3dBm，请计算路径损耗和目标TX Power。只输出公式和结果，不要解释。"
      }
    ]
  }'
```

本次流式测试结果：

- 用时：`103.51s`（`total_duration=103505071843ns`）
- 结束原因：`length`
- 生成 token 数：`1024`
- 流式 chunk 数：`886`
- 返回情况：持续返回 `message.thinking`，但 `message.content` 为空

最后一个流式 chunk 的关键字段：

```json
{
  "done": true,
  "done_reason": "length",
  "total_duration": 103505071843,
  "load_duration": 104590153,
  "prompt_eval_count": 65,
  "prompt_eval_duration": 214788247,
  "eval_count": 1024,
  "eval_duration": 102910939708
}
```

说明：启用 thinking 后，流式接口会把 thinking 过程逐步返回出来，但本次测试在 `num_predict=1024` 的限制下仍未进入最终答案阶段。

## 注意

不要用 `/v1/chat/completions` 关闭 thinking。Ollama 的 OpenAI 兼容接口不支持 `"think": false` 这个控制参数；需要关闭 thinking 时，只使用 `/api/chat`。

## 14B 模型测试

已拉取并测试 `qwen3:14b`：

```json
{
  "name": "qwen3:14b",
  "parameter_size": "14.8B",
  "quantization_level": "Q4_K_M"
}
```

### 禁用 thinking，原始问题

```bash
curl --no-buffer http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:14b",
    "think": false,
    "stream": true,
    "messages": [
      {
        "role": "user",
        "content": "AP1的TX Power是16dBm，邻居AP2收到的RSSI是-65dBm，CCA阈值-82dBm，余量3dBm，请计算路径损耗和目标TX Power。请简洁回答。"
      }
    ]
  }'
```

本次测试结果：

- 用时：`12.74s`（`total_duration=12743719532ns`）
- 加载耗时：`10.43s`（`load_duration=10430721351ns`）
- 生成耗时：`2.18s`（`eval_duration=2184766109ns`）
- 结束原因：`stop`
- 生成 token 数：`44`
- 流式 chunk 数：`44`

真实返回内容：

```text
路径损耗 = 16dBm - (-65dBm) = 81dB
目标TX Power = 16dBm + 3dBm = 19dBm
```

说明：该回答速度很快，但目标 TX Power 计算不符合项目公式约定。

### 禁用 thinking，明确公式

在 prompt 中显式给出项目公式：

```text
路径损耗=TX Power-RSSI；目标接收功率=CCA阈值+余量；目标TX Power=目标接收功率+路径损耗。
```

本次测试结果：

- 用时：`3.51s`（`total_duration=3505176142ns`）
- 加载耗时：`0.10s`（`load_duration=96511823ns`）
- 生成耗时：`3.32s`（`eval_duration=3317532192ns`）
- 结束原因：`stop`
- 生成 token 数：`66`
- 流式 chunk 数：`66`

真实返回内容：

```text
路径损耗 = 16dBm - (-65dBm) = 81dB
目标接收功率 = -82dBm + 3dBm = -79dBm
目标TX Power = -79dBm + 81dB = 2dBm
```

### 启用 thinking

```bash
curl --no-buffer http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:14b",
    "stream": true,
    "options": {
      "num_predict": 1024
    },
    "messages": [
      {
        "role": "user",
        "content": "AP1的TX Power是16dBm，邻居AP2收到的RSSI是-65dBm，CCA阈值-82dBm，余量3dBm，请计算路径损耗和目标TX Power。只输出公式和结果，不要解释。"
      }
    ]
  }'
```

本次测试结果：

- 用时：`49.94s`（`total_duration=49937706753ns`）
- 结束原因：`stop`
- 生成 token 数：`889`
- 流式 chunk 数：`883`
- 返回情况：先流式返回 thinking，最后进入 `message.content`

真实最终内容：

```text
路径损耗 = TX Power - RSSI = 16 - (-65) = 81dB
目标TX Power = CCA阈值 + 余量 + 路径损耗 = -82 + 3 + 81 = 2dBm
```

结论：`qwen3:14b` 明显更快，尤其在禁用 thinking 且公式明确时适合 AP agent 默认调用；但它更依赖 prompt 明确给出计算公式，不能让模型自行解释目标 TX Power 的业务含义。

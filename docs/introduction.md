# 项目计划

> **说明**：本文为项目**初始需求/计划记录**，topology、参数列表、四个场景与协商流程仍为现行口径。
> 运行架构已统一为**纯 OpenClaw**：模型经 OpenClaw provider 调用（默认 PPIO，回退本地 ollama），
> 下方第 1 条「主要依赖 ollama」为初期计划口径。另注：「联合（Co-SR + Co-EDCA）」策略后续已取消，
> 现仅保留 Co-SR 与 Co-EDCA 两类，联合场景按「先处理主导问题」收敛到其一（见聊天示例）。

项目计划如下：

1. 本地大模型部署实现，主要依赖ollama,验收指标：能调用API;
   工作量：1h
2. 基于openclaw实现3个ap-agent,先设置基本的提示词，行为准则，不提供工具，后续4、5、6逐步完善。
   工作量：一天
3. 基于openclaw架构的validator-agent,负责投票完成后核算指标，判断会话是否能结束，还有部分的安全检测。
   工作量：半天
4. 实现全局状态接口，最后带时间戳保证数据时效性，香蕉派POST上传，agentGET获取，做成一个skill。
   工作量：2h
5. Co-SR计算工具模块实现，要实现如下功能：感知距离、量化计算目标、结果安全性验证。
   工作量：一天
6. Co-EDCA计算工具模块实现，要简单很多，设置拥塞等级做映射即可。
   工作量：2h
7. 日志功能，将agent调用工具、获取状态、发言、实施决策的全过程记录下来，采用logging+json的形式。
   工作量：半天
8. 实现决策的执行下发（JSON），完成香蕉派模块的执行脚本，数据回采，结合validator-agent判断会话是否需要继续。
   工作量：一天半
9. dashboard展示平台，使用flask。
   工作量：半天

## 拓扑示意

一个中心DGXspark,通过以太网线连接三个香蕉派AP，三个AP通过无线方式连接三个STA

## 参数列表

| 参数 | 机制 | 作用 |
| --- | --- | --- |
| 可调参数（Co-SR） |  |  |
| TX Power | Co-SR | 发射功率，控制覆盖半径与对邻居BSS的干扰强度；功率越高，OBSS干扰越强，空间复用机会越少 |
| 可调参数（Co-EDCA） |  |  |
| CWmin | Co-EDCA | 竞争窗口下限；值越小初始退避越短，竞争越激烈；AP间协商防止集体取小值加剧碰撞 |
| CWmax | Co-EDCA | 竞争窗口上限；决定重传时退避时间的增长上界；与CWmin共同决定退避分布形态 |
| AIFSN | Co-EDCA | 仲裁帧间间隔数；决定等待信道空闲的最小时隙数；值越小优先级越高；协商统一策略防止某AP长期独占信道 |
| 采集指标（只读，驱动协商触发与效果验证） |  |  |
| Channel busy ratio | 采集 | 信道繁忙时间占活跃时间比例；协商触发的核心判据；需有背景流量才会呈现明显差异 |
| TX retries ratio | 采集 | 重传包比例；协商触发的依据之一，CoEDCA调整效果的直接验证指标；CWmin/CWmax/AIFSN调整后此值应下降 |
| RSSI（邻居AP） | 采集 | CO-SR的感知指标，来判断干扰情况 |
| RSSI（己方STA） | 采集 | 自己关联的STA的信号强度，非常容易读，是降功率的安全下界(需要避免降功率后关联的STA的rssi过低） |
| Noise floor | 采集 | 信道的本底噪声，用于估算SINR，iw survey dump里直接有noise字段，顺手就拿到了 |
| 业务质量指标（只读，协商前后效果对比的直接依据，dashboard中的数据展示） |  |  |
| 吞吐量 | QoS | STA实际接收速率；协商效果最直接的体现； |
| 延迟 | QoS | 端到端往返时延；协商触发判据之一； |
| 丢包率 | QoS | 数据包丢失比例；反映信道质量恶化程度； |

## 核心功能要求

agent架构基于openclaw的多agent架构，官方文档如下

- https://docs.openclaw.ai/gateway/config-agents
- https://docs.openclaw.ai/concepts/agent
- https://docs.openclaw.ai/concepts/multi-agent?utm_source=chatgpt.com
- https://docs.openclaw.ai/concepts/architecture
- https://docs.openclaw.ai/concepts/delegate-architecture

3个ap-agent,负责根据数据协商，下发决策；1个validator-agent,用于核算结果，判断是否已经可以执行完成对话。

对话开始的依据：共享存储区的数值反映某个AP信道很繁忙且重传率高，达到阈值。

三台香蕉派定期向共享存储接口发送新的数据，带时间戳

使用React框架，每次对话输出的结果只有一段话，但可能涉及多轮API调用（需要与工具交互）

第一步为广播信号，三个AP依次发布广播言论，广播自己在共享存储区的自身的指标，严禁在此阶段广播其他AP指标，后续的定时更新也是如此，避免数据不一致和混乱。

第二步是根据数据质量来自动判断发言AP（默认质量最差的），该AP根据自身数据情况来发起提案，调用计算工具，包括自身怎么做，另外几个AP怎么做，需要涉及具体的数值，目前的方案有Co-SR和Co-EDCA。

第三步是其他AP验算决策可能造成的结果，然后决定是否要同意，如果两个都同意提案，则提案通过，决策下发执行，改变香蕉派参数，如果有AP不同意，或效果没达到预期的话，需要继续进行对话。预期指标可提前设定，如果调整后的指标没达到预期需要继续进行对话，直到达到预期指标。

整个过程需要看到：三个AP的对话流程，业务质量指标的变化

## 场景一：静态Co-SR仿真

初始设定三个AP处于静态不对称状态

AP1发射功率处于高位，对邻居BSS干扰较强

AP2处于中等水平

AP3功率最低。

三个STA分别连接对应AP但无明显流量。

AP1的agent检测到自身发射功率过高导致对邻居AP干扰偏强，将当前功率状态上报SPARK，SPARK汇总三个AP的TX Power参数后触发LLM协商，三个AP的agent以自然语言协商各自的空间复用边界，SPARK综合CCA与SINR约束后下发新的TX Power组合，三个AP执行调整，协商结束后AP1功率有所下调，AP3获得更多空间复用机会，协商前后的参数变化可直接对比。

## 场景二：静态Co-EDCA仿真

初始设定三个AP的EDCA参数处于不对称状态

AP1的竞争窗口最小、帧间间隔最短，信道竞争最为激烈

AP2处于均衡水平

AP3竞争窗口较大、帧间间隔较长，整体偏向礼让。

三个STA通过打流工具分别向各自AP注入高、中、低三档背景流量，使三个AP的信道负载产生真实差异。

AP1的agent因信道竞争激烈、重传率偏高触发上报，SPARK汇总三个AP的信道占用率与EDCA参数后启动LLM协商，三个AP的agent协商各自退避窗口与帧间间隔的调整方向，SPARK下发新的CWmin、CWmax、AIFSN组合，香蕉派执行后AP1的重传率与信道占用率应有所下降，协商前后的指标变化可量化对比。

## 场景三：静态Co-SR+Co-EDCA仿真

初始设定四个参数同时处于不对称状态

AP1在TX Power、CWmin、CWmax、AIFSN四个维度上均处于激进高位

AP2居中

AP3在各维度上均偏保守。

三个STA打入不对称背景流量。

AP1的agent同时感知到空间复用边界不合理与信道竞争过激两类问题并一并上报SPARK，SPARK需在同一次LLM协商中处理Co-SR与Co-EDCA两个维度的参数联动，三个AP的agent在协商中权衡功率调整对EDCA竞争的连带影响，SPARK输出一组联合优化的参数组合并下发，难点在于两类参数存在耦合，调整功率会影响邻居感知进而影响竞争行为，协商结束后六个参数整体向均衡方向收敛，综合性能改善幅度作为验证指标。

## 场景四：动态AP1业务骤升

初始设定三个AP处于场景三协商后的收敛稳态，各参数均衡，信道整体运行平稳。

STA1通过打流工具将发送速率从低档突然提升至高档，模拟AP1侧业务骤增。

AP1的agent在下一个采集周期检测到信道占用率与重传率的突变幅度超过触发阈值，立即标记为异常事件并上报SPARK，SPARK判断当前稳态参数已不适配新负载，重新触发三个AP的agent进行动态协商，协商目标是在有限轮次内快速调整AP1的EDCA参数以缓解拥塞，同时评估是否需要联动调整Co-SR参数，SPARK下发调整指令后AP1的拥塞指标应可见回落，整个过程展示系统从稳态被打破到重新收敛的完整动态响应链路，响应速度与收敛轮次作为核心验证指标。

## 聊天示例（场景三：Co-SR + Co-EDCA 联合仿真）

> 数据来自 `MOCK_SCENE_JOINT`，所有数值均由系统工具计算得出，可直接复现。

---

### 第一阶段：广播自身状态

**AP1**

AP1 播报当前状态：

- MAC 参数：TX Power = 20.0 dBm，CWmin = 3，CWmax = 7，AIFSN = 1
- 信道指标：Channel Busy = 82%，TX Retries = 31%
- 感知指标：邻居 AP2 RSSI = -69.0 dBm，邻居 AP3 RSSI = -76.0 dBm（本机扫描值）；己方 STA RSSI = -45.0 dBm；Noise Floor = -92.0 dBm
- 业务质量：吞吐 = 18.4 Mbps，延迟 = 312 ms，丢包 = 1.2%

---

**AP2**

AP2 播报当前状态：

- MAC 参数：TX Power = 20.0 dBm，CWmin = 7，CWmax = 15，AIFSN = 2
- 信道指标：Channel Busy = 55%，TX Retries = 12%
- 感知指标：邻居 AP1 RSSI = -69.0 dBm，邻居 AP3 RSSI = -70.0 dBm；己方 STA RSSI = -48.0 dBm；Noise Floor = -91.0 dBm
- 业务质量：吞吐 = 28.7 Mbps，延迟 = 185 ms，丢包 = 0.4%

---

**AP3**

AP3 播报当前状态：

- MAC 参数：TX Power = 20.0 dBm，CWmin = 15，CWmax = 63，AIFSN = 4
- 信道指标：Channel Busy = 38%，TX Retries = 5%
- 感知指标：邻居 AP1 RSSI = -76.0 dBm，邻居 AP2 RSSI = -70.0 dBm；己方 STA RSSI = -50.0 dBm；Noise Floor = -90.0 dBm
- 业务质量：吞吐 = 34.1 Mbps，延迟 = 98 ms，丢包 = 0.1%

---

### 第二阶段：AP1 发起提案

> 协调者判断：AP1 信道拥塞最严重（Busy 82%，Retries 31%，邻居干扰强），指定 AP1 发起联合提案。
> 协调者已调用工具：Co-SR 连续优化得到各 AP 最优 TX Power；Co-EDCA 映射 AP1=critical / AP2=medium / AP3=low。

**AP1（提案）**

路径判断：**联合（Co-SR + Co-EDCA）**

- Co-SR 触发依据：AP2 和 AP3 对 AP1 的 RSSI 分别为 -69.0 dBm（强干扰）、-76.0 dBm（中等干扰），当前 20 dBm 功率显著越过 CCA 阈值 -82 dBm。
- Co-EDCA 触发依据：AP1 信道繁忙率 82%、重传率 31%，同时超过两项触发阈值。

Co-SR 工具连续优化结果：在满足 CCA、SINR、STA RSSI 约束的前提下，求得各 AP 的最小必要功率调整值。

Co-EDCA 工具映射结果：AP1 = critical，AP2 = medium，AP3 = low。

建议方案：

| AP  | TX Power | CWmin | CWmax | AIFSN | 调整理由 |
|-----|----------|-------|-------|-------|----------|
| AP1 | 20 → **6 dBm** | 7 → **15** | 7 → **63** | 1 → **4** | 强干扰源 + 严重拥塞，功率与退避同步大幅收紧 |
| AP2 | 20 → **6 dBm** | 7 不变 | 15 → **31** | 2 → **3** | 随全局降功率，适度增大退避缓解中等拥塞 |
| AP3 | 20 → **6 dBm** | 15 不变 | 63 不变 | 4 → **2** | 负载最轻，降功率后竞争压力减小，退避可适当放松 |

---

### 第三阶段：投票验算

**AP2（投票）**

验算提案中针对 AP2 的参数：

- TX Power = 6 dBm：降功率后 STA RSSI = -48 + (6 − 20) = **-62.0 dBm**，高于安全下界 -75 dBm ✅；AP1 在 6 dBm 时 AP2 处 CCA = -83.0 dBm，低于阈值 -82 dBm ✅；SINR = 18.09 dB，高于下界 15 dB ✅
- CWmin = 7，在 [3, 1023] ✅
- CWmax = 31，在 [7, 1023]，且 31 > 7 ✅
- AIFSN = 3，在 [1, 15] ✅

所有约束通过。**同意 ✅**

---

**AP3（投票）**

验算提案中针对 AP3 的参数：

- TX Power = 6 dBm：降功率后 STA RSSI = -50 + (6 − 20) = **-64.0 dBm**，高于安全下界 -75 dBm ✅；最强邻居 AP2 在 6 dBm 时 AP3 处 CCA = -84.0 dBm，低于阈值 -82 dBm ✅；SINR = 18.23 dB ✅
- CWmin = 15，在 [3, 1023] ✅
- CWmax = 63，在 [7, 1023]，且 63 > 15 ✅
- AIFSN = 2，在 [1, 15] ✅

所有约束通过。**同意 ✅**

---

### 第四阶段：输出最终决策

**AP1（最终决策）**

```json
{
  "AP1": {"strategy": "联合调整", "tx_power_dbm": 6.0, "CWmin": 15, "CWmax": 63, "AIFSN": 4},
  "AP2": {"strategy": "联合调整", "tx_power_dbm": 6.0, "CWmin": 7,  "CWmax": 31, "AIFSN": 3},
  "AP3": {"strategy": "联合调整", "tx_power_dbm": 6.0, "CWmin": 7,  "CWmax": 15, "AIFSN": 2}
}
```

协商结束

---

### Validator 验算结果

```
🔍 验证 | ✅ 通过 | 验证通过（策略=joint，所有 AP 参数合规）
```

| AP  | tx_power_dbm | CCA_max   | SINR    | STA_RSSI  | CWmin | CWmax | AIFSN | 合规 |
|-----|-------------|-----------|---------|-----------|-------|-------|-------|------|
| AP1 | 6.0 dBm     | -83.0 dBm | 22.8 dB | -59.0 dBm | 15    | 63    | 4     | ✅   |
| AP2 | 6.0 dBm     | -83.0 dBm | 18.1 dB | -62.0 dBm | 7     | 31    | 3     | ✅   |
| AP3 | 6.0 dBm     | -84.0 dBm | 18.2 dB | -64.0 dBm | 7     | 15    | 2     | ✅   |

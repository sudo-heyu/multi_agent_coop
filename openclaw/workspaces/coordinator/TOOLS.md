# 可用工具

这些工具仅服务于兼容 coordinator 路径；默认 `run_openclaw.py` 直接调用 Python `structured_relay`。

## run_fast_negotiation

阶段级快速协商工具。coordinator 应优先调用它，而不是逐轮选择发言人。

工具内部会：
- 重置协商会话并读取最新 AP 状态
- 并发唤醒 ap1/ap2/ap3 生成广播，并按固定顺序记录
- 唤醒提案方完成提案
- 连续唤醒投票方完成验算与投票
- 处理反对、反提案接管、Validator 重试和终止上限
- 返回 transcript、最终 decision、strategy 和 validation

常用参数：
```json
{
  "max_validation_retries": 3,
  "max_turns": 30
}
```

## get_latest_ap_states

只在需要查看当前状态摘要时使用。不要用它替代 `run_fast_negotiation`。

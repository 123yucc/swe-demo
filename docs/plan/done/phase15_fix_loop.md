# Phase15: 未收敛根因排查与验证计划

更新时间：2026-04-11

## 1. 目标与交付

目标：只回答一个问题，为什么 orchestrator 与 deep-search 在当前问题实例上未收敛。

交付：
1. 根因结论清单（按优先级 P0/P1/P2）。
2. 每个根因对应的证据（日志片段、字段状态变化、代码位置）。
3. 非根因项的排除证据（避免误判）。

## 2. 排查范围

代码范围：
1. [src/orchestrator/engine.py](src/orchestrator/engine.py)
2. [src/agents/deep_search_agent.py](src/agents/deep_search_agent.py)
3. [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py)

运行证据范围：
1. [workdir/swe_issue_001/run_log_main_entry.txt](workdir/swe_issue_001/run_log_main_entry.txt)
2. [workdir/swe_issue_001/outputs/evidence_cards.json](workdir/swe_issue_001/outputs/evidence_cards.json)

## 3. 分层排查（代码层面）

### C1. deep-search 输出解析失败

判定条件：
1. 出现 post_tool_use_hook 但无可解析 section。
2. exact_code_regions 仍为空或格式非法。

证据点：
1. [src/orchestrator/engine.py](src/orchestrator/engine.py#L224)
2. [src/orchestrator/engine.py](src/orchestrator/engine.py#L280)

### C2. Hook 入栈或命中条件失败

判定条件：
1. deep-search 已调用，但无 post_tool_use_hook 相关日志。
2. hook 出现但 update_localization 未被调用。

证据点：
1. [src/orchestrator/engine.py](src/orchestrator/engine.py#L360)
2. [src/orchestrator/engine.py](src/orchestrator/engine.py#L393)

### C3. 持久化链路失效（调用了但没落盘）

判定条件：
1. update_localization 被调用，但 evidence_cards.json 无增量。
2. 返回计数与文件内容不一致。

证据点：
1. [src/tools/ingestion_tools.py](src/tools/ingestion_tools.py)
2. [workdir/swe_issue_001/outputs/evidence_cards.json](workdir/swe_issue_001/outputs/evidence_cards.json)

### C4. 子代理长时间工具调用但无收尾消息

判定条件：
1. 长时间只有 Grep/Glob/Read/TodoWrite。
2. 无 ResultMessage 或 task completed 收尾。

证据点：
1. [workdir/swe_issue_001/run_log_main_entry.txt](workdir/swe_issue_001/run_log_main_entry.txt)

### C5. recovery 机制未有效改变行为

判定条件：
1. 触发 recovery 后，仍无 deep-search 有效推进。
2. 或 recovery 根本未触发但必填字段仍缺失。

证据点：
1. [src/orchestrator/engine.py](src/orchestrator/engine.py#L697)
2. [src/orchestrator/engine.py](src/orchestrator/engine.py#L711)

## 4. 分层排查（设计层面）

### D1. 下发任务粒度过大

判定条件：
1. TODO 目标过宽，没有字段级完成定义。
2. deep-search 反复扩散搜索而不收敛到定位字段。

### D2. 上下文注入噪声偏高

判定条件：
1. 当前证据上下文过长，掩盖本轮目标。
2. 子代理输出出现“持续探索”而非“完成闭环”。

### D3. Phase1 闭环契约与子代理输出契约不一致

判定条件：
1. orchestrator 需要的字段与 deep-search 可稳定产出的字段不对齐。
2. 导致“看似有进展但永远不满足闭环”。

## 5. 执行顺序

1. 先做 C4 事实确认（是否真的没有收尾）。
2. 再做 C1/C2/C3（解析、hook、落盘链路）。
3. 最后做 C5 + D1/D2/D3（机制与设计对齐）。

## 6. 30分钟续跑与断点续采策略

策略目标：出现 API 超时后，不清空历史，不丢上下文，直接在同一证据目录持续追加采样。

规则：
1. 日志文件只追加，不覆盖。
2. 每次重试使用同一个 output-dir，即 workdir/swe_issue_001/outputs。
3. 重试前先读取上次日志末尾，记录最后一个已观察事件（作为断点）。
4. 每30分钟触发一次重试，直到出现可判定根因证据。

断点定义：
1. 最近一次 tool_use name=Agent subagent_type=deep-search 行。
2. 最近一次 tool_use name=TodoWrite 行。
3. 最近一次 ResultMessage 或异常行。

## 7. 通过标准

满足以下任一条即可输出“未收敛根因结果”：
1. 连续两轮运行都复现同一失败模式，且证据链完整。
2. 单轮运行直接出现确定性失败信号（例如 recovery 后仍无推进、hook 命中失败）。

## 8. 本计划对应当前实现状态

当前已经完成：
1. exact_code_regions 格式校验与强闸门。
2. recovery 二次失败硬中止。
3. 运行后 mandatory 字段 fail-fast。

当前待拿结果：
1. 未收敛最终根因结论（你要求的唯一输出）。

## 9. 本轮实测结论（已拿到）

结论：本轮未收敛的直接原因是 API 限流中断，不是模型主动闭环结束。

证据：
1. 出现连续 api_retry 事件，见 [workdir/swe_issue_001/run_log_phase15_loop.txt](workdir/swe_issue_001/run_log_phase15_loop.txt#L97)
2. 明确 429 rate_limit_exceeded，见 [workdir/swe_issue_001/run_log_phase15_loop.txt](workdir/swe_issue_001/run_log_phase15_loop.txt#L117)
3. ResultMessage 标记 is_error=True，且返回 429，见 [workdir/swe_issue_001/run_log_phase15_loop.txt](workdir/swe_issue_001/run_log_phase15_loop.txt#L119)
4. summary 显示 pipeline_complete_text=False，见 [workdir/swe_issue_001/run_log_phase15_loop.txt](workdir/swe_issue_001/run_log_phase15_loop.txt#L120)
5. 运行最终被 mandatory fail-fast 中止（字段仍缺失），见 [workdir/swe_issue_001/run_log_phase15_loop.txt](workdir/swe_issue_001/run_log_phase15_loop.txt#L154)

补充判断：
1. deep-search 已经被派发（deep_search_calls=1），说明不是“未派发”问题。
2. 由于限流在 deep-search 过程中触发，未产出可落盘闭环证据，导致后续强校验失败并退出。

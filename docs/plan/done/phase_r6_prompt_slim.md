# Phase R6: Prompt 瘦身 — 拼接代替预写

## 目标
子 agent 的 prompt 改为代码动态拼接当前状态片段，移除大段静态规则描述；每个 agent 的 prompt 只保留语义指导。

## 改动
- deep-search prompt：代码函数 `build_deep_search_prompt(todo, relevant_fields)` 拼接 TODO + 仅相关 evidence 字段，移除 prompt 中重复的状态序列化协议说明（已由 structured output 保证）
- closure-checker prompt：移除机械性校验规则（已由代码预检），只保留"评估事实对齐"语义指令
- patch-planner/generator prompt：移除依赖顺序说明（代码控制调度顺序），移除"必须调用 tool"指令（代码校验调用结果）
- 各 prompt 目标长度 < 1000 字符（当前 2000-4000）

## 验收
- 各 agent prompt 长度显著缩减
- 不再在 prompt 中重复代码已保证的规则

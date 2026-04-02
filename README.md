# 基于证据闭环的软件工程修复 Agent

基于 Claude Agent SDK 的软件工程缺陷修复 Agent，采用 Evidence Closure 理论框架。

## 开发硬性规则
时刻注意清理历史遗留，注意注释和现有功能的匹配性。不要使用降级策略，严禁出现“双重路径”。保持运行入口始终在src/main.py，没有接入主流程的代码等于不存在。

## 理论核心

修复失败的主要原因不是 agent 不会生成 patch，而是缺乏判断当前信息是否足以支持 patch commitment 的能力。本系统通过四类必备 evidence 的闭环检查来解决这个问题：

1. 症状证据（Symptom Evidence）：现在坏在哪里，修好后应表现为什么样。
2. 定位证据（Localization Evidence）：应该改哪里。
3. 约束证据（Constraint Evidence）：什么修改才算正确，什么不能改坏。
4. 结构证据（Structural Evidence）：哪些位置必须一起改，修改之间是什么依赖关系。

## 当前架构


## 目录结构

workdir/{instance_id}/


## 可用命令

支持phase1-only/phase2-only 参数


## 阶段状态

1. Phase 1：已实现
2. Phase 2：已实现
3. Phase 3：未实现
4. Phase 4：未实现
5. Phase 5：未实现
6. Phase 6：未实现

后续将继续增强 Phase1/Phase2 的抽取质量与闭环策略精度。

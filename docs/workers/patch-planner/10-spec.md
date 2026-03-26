# patch-planner 规格

## 角色

基于已闭环证据生成有序、低风险的 patch 计划。

## 输入

- closure 决策（必须为 allow）
- 最新 evidence cards

## 输出

- `plan/` 目录下的 patch 计划产物

## 验收标准

- 编辑顺序有明确先后并具备理由。
- 包含风险项与回滚说明。
- 验证清单可直接执行。

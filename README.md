# 基于证据闭环的软件工程修复 Agent

基于 Claude Agent SDK 的软件工程缺陷修复 Agent，采用 Evidence Closure 理论框架。

## 理论核心

修复失败的主要原因不是 agent 不会生成 patch，而是缺乏判断"当前信息是否足以支持 patch commitment"的能力。本系统通过四类必备 evidence 的闭环检查来解决这个问题：

1. **症状证据（Symptom Evidence）**：现在坏在哪里，修好后应表现为什么样
2. **定位证据（Localization Evidence）**：应该改哪里
3. **约束证据（Constraint Evidence）**：什么修改才算正确，什么不能改坏
4. **结构证据（Structural Evidence）**：哪些位置必须一起改，修改之间是什么依赖关系

## 项目结构

```
workdir/
└── {instance_id}/                        # 每个 SWE-Bench instance 独立隔离
    ├── repo/                             # clone 下来的目标仓库
    ├── artifacts/                        # Phase 1 输入
    │   ├── problem_statement.md
    │   ├── requirements.md
    │   ├── new_interfaces.md
    │   └── expected_and current_behavior.md
    ├── evidence/                         # Phase 2 产出
    │   ├── symptom_card.json
    │   ├── localization_card.json
    │   ├── constraint_card.json
    │   ├── structural_card.json
    │   └── card_versions/
    ├── closure/                          # Phase 3 产出
    ├── plan/                             # Phase 4 产出
    └── patch/                            # Phase 5 产出
```

## 目前可用命令

  运行 Phase 1（生成初始证据卡）:
  python main.py face_recognition_issue_001 --phase1-only
  python main.py research_agent_issue_002 --phase1-only

  运行 Phase 2（增强证据卡，添加精确位置和依赖分析）:
  python main.py face_recognition_issue_001 --phase2-only
  python main.py research_agent_issue_002 --phase2-only

  运行完整动态调度工作流（推荐）:
  python main.py face_recognition_issue_001 --dynamic
  python main.py research_agent_issue_002 --dynamic

  其他有用参数:
  - --resume - 从上次中断处恢复
  - --fail-fast - 失败时立即停止
  - --from-phase phase2 - 从指定阶段开始


## 开发阶段

- [×] Phase 1：Artifact Parsing（产物解析）
- [×] Phase 2：Artifact-to-Evidence Extraction（证据提取）
- [ ] Phase 3：Closure Checking（闭环判定）
- [ ] Phase 4：Patch Planning（修复规划）
- [ ] Phase 5：Patch Generation（补丁生成）
- [ ] Phase 6：Validation/Replan（验证与重规划）

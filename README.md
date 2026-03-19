# Evidence-Closure-Aware Software Engineering Repair Agent

基于 Claude Agent SDK 的软件工程 Issue 修复 Agent，采用 Evidence Closure 理论框架。

## 理论核心

修复失败的主要原因不是 agent 不会生成 patch，而是缺乏判断"当前信息是否足以支持 patch commitment"的能力。本系统通过四类必备 evidence 的闭环检查来解决这个问题：

1. **Symptom Evidence**: 现在坏在哪里，修好以后应该表现成什么样
2. **Localization Evidence**: 应该改哪里
3. **Constraint Evidence**: 什么修改才算正确，什么不能改坏
4. **Structural Evidence**: 哪些位置必须一起改，修改之间是什么依赖关系

## 项目结构

```
workdir/
└── {instance_id}/                        # 每个 SWE-Bench instance 独立隔离
    ├── repo/                             # clone 下来的目标仓库
    ├── artifacts/                        # Phase 1 输入
    │   ├── problem_statement.md
    │   ├── requirements.md
    │   ├── interface.md
    │   └── 
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

## 开发阶段

- [×] Phase 1: Artifact Parsing
- [×] Phase 2: Artifact-to-Evidence Extraction
- [ ] Phase 3: Closure Checking
- [ ] Phase 4: Patch Planning
- [ ] Phase 5: Patch Generation
- [ ] Phase 6: Validation/Replan

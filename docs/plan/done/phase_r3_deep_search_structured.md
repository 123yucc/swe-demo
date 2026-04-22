# Phase R3: Deep-search 报告结构化

## 目标
deep-search 从 markdown 报告 + 正则解析改为结构化输出，消除格式漂移导致的解析失败。

## 改动
- 新增 `src/models/report.py`：定义 `DeepSearchReport` Pydantic model，字段对应当前 markdown 各 section（exact_code_regions, suspect_entities, call_chain_context 等）
- deep_search_agent.py：使用 `output_format` 返回 `DeepSearchReport`
- engine.py：移除 `parse_deep_search_report` 及全部正则解析函数（`_extract_section`, `_extract_list_items`, `_extract_exact_lines_block` 等），`_persist_deep_search_findings` 改为直接从 `structured_output` 读取并调用 `update_localization.handler`
- 保留 `DeepSearchReport` 到 `update_localization` 参数的映射逻辑（纯代码，无正则）

## 验收
- 消除所有 `_extract_*` 正则解析函数
- deep-search 结果可被代码直接反序列化并持久化

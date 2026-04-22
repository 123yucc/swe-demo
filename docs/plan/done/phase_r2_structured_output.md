# Phase R2: Closure-checker 结构化输出

## 目标
closure-checker 从自由文本输出改为 SDK structured output，消除字符串匹配解析的脆弱性。

## 改动
- 新增 `src/models/verdict.py`：定义 `ClosureVerdict` Pydantic model，含 `verdict: Literal["CLOSURE_APPROVED", "EVIDENCE_MISSING"]`、`missing: list[str]`、`suggested_tasks: list[str]`
- closure_checker_agent.py：使用 `output_format` + `ClosureVerdict.model_json_schema()`，与 parser_agent 同模式
- engine.py：`_track_closure_checker_verdict` 改为从 `ResultMessage.structured_output` 读取，不再做 `"CLOSURE_APPROVED" in markdown` 匹配
- 移除 `_extract_tool_response_text` 中的 verdict 解析逻辑

## 验收
- closure-checker 返回结果可被代码直接反序列化为 `ClosureVerdict`
- 不再有任何字符串匹配判断 verdict 的代码

# Changelog

## 0.1.3

- 修正 _manifest.json 的版本范围：host_application.max_version 改为 1.3.99，sdk.max_version 改为 2.9.99
- 同步 config_version 与 README 版本号

## 0.1.2

- **改为解析真实参数**：识别「调用reply工具：- msg_id: … / - expression_intent: …」这类文字说明，
  按平台已注册工具的 `parameters_schema` 过滤字段、收敛类型（含 enum）、补齐必填项，组装成真实调用
- **不再把引号内容当台词**：麦麦的 `reply` 工具没有台词参数（台词由 replayer 生成），
  旧版会把「摸摸希罗」这类引号内容误当回复；现在除非工具真的声明了文本参数，否则绝不编台词
- `msg_id` 会从「使用当前消息 msg_id="-123"」这类句子里抠出纯 id
- `reply_style` 等被写进 `expression_intent` 的字段会自动提升到顶层
- 新增配置：`tool_names`、`enabled_spec_parsing`、`synth_reply_reference`
- 移除 0.1.1 的 `reply_keywords` / `allow_fallback` / `fallback_text`（不再靠关键词猜意图）

## 0.1.1

- 修正误判：模型明确决定静默（`不进行工具调用` / `保持静默` / `wait` 等）时不再被兜底成回复
- `reply_keywords` 收敛为「明确调用回复工具」的强信号，移除泛词「回复 / 回应」
- 新增 `silence_keywords` 否定信号拦截
- 新增 `allow_fallback`（默认 `false`）：提取不到台词时不发送兜底句

## 0.1.0

- 首个版本：监听 `maisaka.planner.after_response`，把「决定调用回复工具但只输出文字」的
  planner 输出改写为一次真实工具调用
- 工具名与参数名全部取自 `ctx.tool.get_definitions()`，不伪造工具
- 支持 `dry_run` 观察模式

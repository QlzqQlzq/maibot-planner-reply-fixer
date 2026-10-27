# Changelog

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

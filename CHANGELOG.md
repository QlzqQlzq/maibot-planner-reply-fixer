# Changelog

## 0.1.7

- 修静默误判：模型写「无需调用工具，直接通过 reply 发出回应」时，过去会被当成「不说话」而跳过。
  现在静默判定分两级——「保持安静 / 不说话 / stay silent」等强表述优先；
  「无需调用 / 不调用」这类弱否定，如果同一段里明确说要走 reply，就按「要回复」处理
- 跳过时打印输出摘要（前 240 字），方便排查「为什么没兜底」
- 本轮工具列表日志继续保留


## 0.1.6

- **白名单真正生效**：模型散文里点名的工具必须在 `tool_names` 里，否则一律不补（此前可以点名任意已注册工具）
- 不再回退调用 `tool.get_definitions`（该能力拿不到内置 `reply`），工具定义只取自每轮 `before_request` 的 `tool_definitions`；拿不到就什么都不做
- `logical_turn_id` 取不到时直接放弃（宿主 `FunctionCallItem` 要求非空，之前写 null 会被丢弃并刷 warning）
- 回写时保留 `ReasoningItem` / provider 活动项，只替换 assistant 正文，避免把推理从历史里删掉
- 必填校验更严：`required` 缺失或写成字符串时也按安全策略处理；`reply_reference` 缺失直接放弃
- 静默判断改为内置规则（不可配置）+ 可选补充词，补齐中文/英文常见说法
- `msg_id` 只认 `msg_id=123` / `msg_id: 123` 形式，避免把引用里的数字当目标消息
- 嵌套缩进改为按原始缩进长度比较，修掉 tab 与空格混用时丢嵌套的问题
- hook 超时改为继承宿主默认值（原来 3s 内含嵌套 RPC，超时会触发熔断）
- `models` 默认改为空（不限制），并在未命中时打日志，避免「装了但没反应」查不到原因
- 日志提升：每轮工具列表、白名单拒绝、静默命中、缺参等都有明确记录
- 去掉 `i18n.locales_path`（仓库内并无语言资源）

## 0.1.5

- 修正工具定义解析：`tool.get_definitions` 的真实返回是 `{"success": true, "tools": [{"name", "definition": {"parameters_schema"}}]}`，旧代码按 OpenAI 风格找 `function.parameters` 找不到，导致兜底链路一直空转
- 工具定义改为从 `maisaka.planner.before_request` 的 `tool_definitions` 缓存（每轮真实工具列表，含宿主内置 reply），`tool.get_definitions` 仅作回落
- `logical_turn_id` 兜底值由 `""` 改为 `None`（宿主要求非空字符串或 null）
- `_model_in_scope()` 读取宿主配置异常时改为不生效（fail-closed），避免「只对小模型生效」悄悄退化

## 0.1.4

- 不再把 `config.toml` 提交进仓库（改由 Runner 按 config_model 生成），并加入 `.gitignore`，避免更新插件时冲突
- `_manifest.json` 的 `capabilities` 补上 `config.get`（`_model_in_scope()` 会读取宿主的 planner 模型名单，之前未声明会被拒，导致「只对小模型生效」静默失效）
- `config_version` 默认值与发布版本对齐

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

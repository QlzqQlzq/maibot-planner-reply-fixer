# Changelog

## 0.1.15

- README 的「装好了」示例日志改成与 `on_load()` 实际输出一致
  （`已加载，工具白名单=['reply'] 散文模式=True dry_run=False`）
- 散文模式不再悄悄回落到白名单里的另一个工具：
  当 `prose_tool_name` 不在 `tool_names` 内时，记一条 warning 并跳过，
  避免 `tool_names=["send_poke"]` 这种配置下被误用来戳人


## 0.1.14

- 撤掉失效的 `fixer.models` 选项与 `config.get` 能力声明：
  `ctx.config.get("model_task_config.planner.model_list")` 读的是 bot_config，
  而 planner 模型列表在 model_config 里，永远取到默认值 —— 一旦用户填了 `models`，
  插件反而每轮都放弃。现在统一为「对所有 planner 模型生效」：
  - 删除 `models` 配置项与 `_model_in_scope()`
  - `capabilities` 置空（插件不再需要任何能力）
  - README / manifest 描述同步
- 日志不再打印完整 `args`：只记工具名与参数键名，避免把生成的参考文本写进日志


## 0.1.13

- `reply_reference` 生成重写：
  - 去掉尾部括注（含被截断未闭合的「（本轮…」）
  - 只丢「无需调用工具 / 不必回复」这类否定分句，保留前面的方向描述
  - 截断只发生在句末或逗号处，不再留半句话
  - 清洗完不足 8 字时回退到原始文本
- `max_reference_chars` 默认 160 → 200


## 0.1.12

- **修 BLOCKER：msg_id 来源错误**。after_response 只拿得到模型输出，拿不到上下文；
  现在在 `before_request` 把上下文按会话缓存下来，从里面的 `<message msg_id="...">` 取最新一条，
  并跳过 `is_self_message="true"` 与自己引用的旧消息。
- **修 BLOCKER：否定语义被当成要回复**。「不需要回复 / 不应该回应 / 不适合接一句」现在优先判为不说话，
  不再把否定句塞进 `reply_reference` 传给 replayer。
- 散文模式固定使用 `prose_tool_name`（默认 `reply`），不再盲取 `tool_names` 第一项
  （此前配 `["send_poke","reply"]` 会真的去戳人）。
- `enabled_spec_parsing=false` 时整个插件不工作（与 README 一致）。
- 修 `_squash` 未定义导致的整次兜底被吞；`reply_reference` 去掉 msg_id、列表行、
  「决策：/分析：/决定：」等标记。
- 第三人称过滤只认多字词，避免「其他/其它」误伤口语里的「他/她」。
- 模型输出正文的日志降到 debug 级。


## 0.1.11

- 移除调试用的 `[diag]` 日志（避免把聊天原文写进日志）
- 意图识别加「第三人称」排除：`对方应该回复你` 这类描述不再当成「我要说话」
- 强静默词补充：`不用搭理 / 不必理会 / 无需参与 / 无需介入 / 不必回复`
- manifest 描述改为与实现一致：默认对所有模型生效，可用 `fixer.models` 限定
- 注释里的示例消息 id 已脱敏


## 0.1.10

- 修 msg_id 提取：宿主部分场景把引号渲染成 `msg_id=\"-123\"`，旧正则取不到，导致散文模式放弃
- 放宽回复意图识别：`可以/应该/适合/直接…回复|回应|接一句|搭话` 也算「要说话」
- `min_text_chars` 默认 20 → 10（短句也能兜底）
- 群聊提示词允许「没人点名时偶尔接一句」


## 0.1.9

- 新增「散文模式」：模型只写「调用reply工具，语气轻松自然」这类文字时，插件自己从上下文的
  `<message msg_id="-...">` 里取出目标消息 id，用这段文字压成 `reply_reference`，硬拼一次 reply 调用。
  这样即使模型不按格式输出参数，也能让角色真的开口（默认开启，可用 `fixer.prose_mode` 关掉）
- 正文只取 assistant 的 part，避免把上下文消息原文混进 `reply_reference`


## 0.1.8

- 诊断增强：跳过时区分「散文但表达了回复意图」与「散文无回复意图」，并把输出摘要记进日志
- 便于统计「模型写散文但不调工具」的真实占比；功能逻辑与 0.1.7 一致


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

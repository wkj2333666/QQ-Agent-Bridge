# 长期记忆提取格式

## 硬性输出格式

**输出必须是合法 JSON，不能包含任何其他内容。** 没有 markdown 代码块、没有解释文字、没有前缀或后缀。

### 有操作时（一个或多个记忆提案）：

```json
{"operations":[{"operation":"add","source_ids":[1],"subject_kind":"user","subject_id":"u1","category":"preference","content":"喜欢简洁回答","confidence":0.91,"status":"active","sensitivity":"normal","source_kind":"self_statement","explicit_memory":false,"decay_exempt":false,"expires_at":null}]}
```

### 无操作时（没有值得记录的记忆）：

```json
{"operations":[]}
```

## 禁止的输出

以下输出会导致整个 review 失败、所有 source 丢失、必须从头重试：

- `[no operations]` — 错误！必须是 `{"operations":[]}`
- `[no new memories]` — 错误！必须是 `{"operations":[]}`
- `[none]` — 错误！必须是 `{"operations":[]}`
- 任何以 `[` 开头、以 `]` 结尾但不是合法 JSON 数组的文本
- 任何包含解释、markdown、代码块的输出
- 任何以自然语言开头或结尾的输出（如 `根据审核...`、`以下是结果：`）

## 为什么格式这么严格

你的输出会直接被 `json.loads()` 解析。如果解析失败，整批 source 会被标记为 malformed_output，所有未处理的 source 保持 pending 状态等待重试。反复失败会导致 source 超过最大重试次数后被丢弃。

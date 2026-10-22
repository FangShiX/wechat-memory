# WeChat Memory

本地优先的微信聊天导出分析工具。将不同来源的记录转换为统一消息，按会话整理上下文片段、季度概览、概念出现轨迹和原文证据；生成可直接打开的离线网页。

**默认不联网、不需要 API Key。** 可选 AI 标注和问答必须显式加 `--ai`，才会把所选消息原文、会话和发送者 ID 发给你配置的模型端点。

## 安装与运行

需要 Python 3.10 或更新版本，支持 Windows、macOS、Linux。

```bash
python -m pip install .
wechat-memory run examples/demo.jsonl --output outputs/demo
wechat-memory ask "公共交通" --workspace outputs/demo
```

浏览器打开 `outputs/demo/dashboard.html`，可查看季度概览、概念轨迹和分页搜索原文。也可以用 `python -m wechat_memory` 代替 `wechat-memory`。

## 导入你的记录

```bash
wechat-memory run export.jsonl --output outputs/my-chat
wechat-memory run chat-a.csv chat-b.json --output outputs/multiple
wechat-memory run my-exports --output outputs/batch
wechat-memory run conversation.txt --sender "某发送者ID或昵称" --output outputs/one-person
wechat-memory ask "城市交通" --workspace outputs/multiple --chat "会话ID" --quarter 2025-Q1
```

目录导入只读取该目录下支持的文件，不递归。输出目录必须不存在，避免覆盖已有分析。导入默认遇到坏记录就停止，不留下半成品；`--skip-invalid` 可跳过坏行并记录其文件和行号。统计见 `import_report.json`。

| 来源 | 支持范围 |
| --- | --- |
| JSONL / NDJSON | 每行一条消息；兼容 `_type: message`，跳过 header/member 元数据 |
| JSON | 消息数组，或 `messages` / `records` / `data` 包裹；支持 ChatLab 风格 `meta` + `messages` |
| CSV / TSV | 自动识别常见中英文列名、分隔符，保留引号中的换行；可映射自定义列 |
| TXT | `2025-01-10 09:00:00 张三` 后接多行正文，或同一行 `2025-01-10 09:00:00 张三: 正文`；也支持 `张三 2025-01-10 09:00:00` 后接正文 |
| SQLite | 仅支持已解密、已导出的副本，自动选择唯一兼容表；多表用 `--table` 指定 |

这不是微信数据库解密或账号提取工具。不支持直接读取加密数据库、微信备份包、HTML、DOCX 或媒体文件。不同导出工具的版本可能改变结构；以上是字段和格式兼容范围，不能保证所有第三方版本。SQLite 导出请先关闭源程序并复制数据库及对应 WAL 文件，避免丢失未归档消息。

常见列名：时间 `timestamp/time/createTime/CreateTime/StrTime/时间`；正文 `content/text/StrContent/内容`；发送者 `sender/senderId/发送者`；会话 `chat_id/chatId/StrTalker/会话`。支持秒、毫秒、微秒、纳秒和 ISO 时间；无时区时间按 `--timezone` 解释，默认 `Asia/Shanghai`，季度也使用此时区。DST 重复或不存在的本地时间需在输入中写明 UTC 偏移。

UTF-8、带 BOM 的 UTF-8/UTF-16 和 GB18030 自动处理；其他编码通过 `--encoding` 指定。时间、正文等自定义字段可写一个映射文件：

```json
{"timestamp":"sent_at","text":"body","sender":"author","chat_id":"room","platform_id":"message_key"}
```

```bash
wechat-memory run custom.csv --mapping mapping.json --output outputs/custom
wechat-memory run exported.sqlite --table messages --output outputs/sqlite
```

无会话字段时使用文件名（去扩展名）作为会话 ID。合并同一会话的多个导出分片时，用 `--chat-id` 显式指定；不要用它合并不同会话。消息 ID 带会话命名空间；重复导出按平台 ID 去重，无平台 ID 时按会话、时间、发送者、类型和正文去重。后一种方式无法区分同秒发送的完全相同消息。相同 ID 对应不同内容时拒绝导入。

发送者缺失时保留 `unknown`；只有 `IsSender` 时能识别自己发出的消息，但不会猜测其他成员身份。群聊分析保留会话、发送者和原文，不把整段群聊归为一个人的观点。需要个人分析请用 `--sender`。

## 可选 AI

设置 `OPENAI_MODEL`、`OPENAI_API_KEY`，可用 `OPENAI_BASE_URL` 指向兼容 Chat Completions JSON 输出的端点。默认端点是 `https://api.openai.com/v1`；本地模型可使用 `http://localhost:端口/v1`，本地端点允许无 Key。

```bash
wechat-memory run export.json --ai --output outputs/with-ai
wechat-memory ask "公共交通讨论了哪些问题" --ai --workspace outputs/with-ai
```

`run --ai` 对每个含文本的片段请求标注；`ask --ai` 只发送检索到的消息。模型返回的引用 ID 和逐字引文必须匹配输入，否则重试并报错；不把生成失败伪装成成功。最多尝试两次，每次超时 60 秒；单次原文超过 20,000 字符会要求缩小范围，不会静默截断。离线检索是词项匹配，不是语义搜索；没有匹配时直接返回证据不足。模型置信度未经校准，引用正确也不保证概括正确。

## 本地数据与公开范围

工作目录包含原文、发送者信息、规范化消息、片段、统计、概念轨迹和网页，请当作私人文件保存。没有遥测、云同步、自动媒体下载；打开网页也不会发起网络请求。没有自动脱敏功能，自由文本中的个人信息仍会保留；不要直接公开自己的分析页面。词项复现不代表思想变化、因果或心理诊断；语音/图片仅保留导出内容与占位符。

仓库和发行包只包含代码、必要配置、测试与完全虚构的样例。原型中的个人聊天、知识图谱、提示词、标签映射、报告和旧输出均未纳入公开版本。Git 使用文件白名单，新增文件需要有意修改白名单后才能纳入；`.private/`、输入、输出、数据库、环境变量文件与构建缓存默认忽略。MIT License。

开发验证：`python -m unittest discover -s tests -v`。

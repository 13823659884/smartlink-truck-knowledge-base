# 中重卡用养修保知识库与知识图谱

本目录依据《车辆知识问答助手产品方案说明》搭建，包含两类可直接使用的数据能力：

1. **专有知识库**：解析 `0729中重卡知识库素材` 下的 PDF、Word、PPT 和 Excel 文档，按车系、用/养/修/保、燃料类型、版本及来源位置建立 SQLite 全文索引和 Qdrant 中文语义向量索引。
2. **知识图谱**：将 `knowledge_graph/output` 中的实体和关系导入 SQLite，并以标准三元组 `主体—关系—客体` 提供查询。

## 目录

- `config.json`：知识库、车系和场景配置。
- `schema.sql`：文档、分块、向量、实体、三元组、会话和纠偏记录的数据模型。
- `scripts/build_kb.py`：可重复构建脚本；按文件内容哈希复用解析结果。
- `scripts/query_kb.py`：命令行混合检索与图谱查询。
- `scripts/qdrant_store.py`：使用本地 BGE 中文向量模型构建、增量同步和查询 Qdrant。
- `scripts/serve.py`：本地知识问答与管理接口。
- `scripts/doubao_client.py`：火山方舟智能体适配器，只把检索到的企业证据发送给模型。
- `scripts/source_preview.py`：为命中的 PDF 页、PPTX 幻灯片和文档图片生成受控预览。
- `scripts/ocr_engine.py`：使用本地 RapidOCR ONNX 识别扫描 PDF 和用户上传图片。
- `scripts/diagnosis.py`：故障码提取、证据充足度和引导式诊断卡片。
- `web/`：本地问答界面。
- `output/knowledge_base.db`：构建后的知识库数据库。
- `output/build_report.json`：构建质量报告。
- `output/extraction_cache/`：文档解析缓存；文件内容不变时不会重复解析。
- `output/unparsed_files.csv`：需 OCR 或格式转换的文件清单。
- `output/triples.csv`：标准三元组导出。

## 构建

```powershell
python .\scripts\build_kb.py
```

构建会同步更新 `output/qdrant`。Qdrant 使用本地持久化模式，不需要 Docker；向量模型缓存在 `output/models`，文档不会上传到第三方。首次构建需要处理全部分块，之后只向量化新增或内容发生变化的分块。由于本地 Qdrant 采用文件锁，重建知识库前请先停止 `scripts/serve.py`，完成后再启动服务。

构建会先保存会话、消息和反馈，再重建文档索引并恢复这些运行数据。解析缓存以文件内容 SHA-256 为键，因此新增或替换资料后仍可直接运行同一条命令，未变化文件不会再次解析。不要手工删除 `output/extraction_cache`，除非需要强制重做全部文档解析。

扫描 PDF 会在普通文本提取失败后自动进入本地 OCR。OCR 运行库位于 `tools/python_packages`，资料不会上传到第三方。当前 76 份扫描图纸均已执行 OCR，其中 60 份形成可检索分块；其余 16 份作为 `image_only` 保留原图和预览，不根据模糊内容生成答案。OCR 结果同样按文件哈希缓存。

## 查询

```powershell
python .\scripts\query_kb.py --question "BMS故障码怎么排查？" --scene 修
python .\scripts\query_kb.py --question "SAA38289有哪些图纸？"
python .\scripts\query_kb.py --question "JH6的保养要求是什么？" --vehicle-series JH6 --scene 养
```

## 启动本地服务

```powershell
python .\scripts\serve.py
```

浏览器打开 `http://127.0.0.1:8008`。服务仅监听本机地址，原始知识文档不会对外发布。

移动端小程序风格入口：`http://127.0.0.1:8008/mini/`。该页面是本地手机界面演示，不需要微信 AppID，和电脑端共用同一知识库、Kimi 智能体、会话及反馈接口。

## 接入火山方舟 Kimi 智能体

复制 `.env.example` 为 `.env`，填入从火山方舟控制台创建的 API Key：

当前配置使用 Agent Plan 的 OpenAI 兼容网关：

```env
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
ARK_MODEL=kimi-k3
```

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
python .\scripts\serve.py
```

智能体接口：

```http
POST /api/agent/chat
Content-Type: application/json

{
  "question": "刹车片磨损有什么表现，应该怎么处理？",
  "vehicle_series": "JH6",
  "scene": "修",
  "conversation_id": "可选的连续会话ID"
}
```

也可以继续调用 `POST /api/search`，并增加 `"use_agent": true`。服务端执行顺序为：知识库召回、图谱扩展、Kimi 基于证据组织答案。API Key 只保存在后端 `.env`，不能放进小程序代码。

### 流式回答与回答模式

小程序使用 `POST /api/search/stream`，响应格式为逐行 JSON（NDJSON）：

- `status`：当前处理阶段。
- `meta`：检索耗时、来源数量和 Qdrant 状态。
- `delta`：模型新增回答文字。
- `mode_fallback`：快速模型不可用时自动切换深度模式。
- `done`：完整答案、诊断、文档、图片、会话和耗时。

请求中的 `answer_mode` 支持：

- `fast`：豆包 Lite，证据和对话上下文更精简，适合日常查询。
- `deep`：Kimi K3，适合复杂故障与完整诊断。

```json
{
  "question": "刹车片磨损怎么检查？",
  "scene": "修",
  "answer_mode": "fast",
  "use_agent": true
}
```

搜索响应还会返回：

- `related_documents`：命中的相关文档、资料位置和受控原文链接。
- `related_images`：命中的 PDF 页面、PPTX 幻灯片或文档图片预览。
- `answer_images`：直接嵌入当前智能体回答下方的最多 4 张相关图片或页面。
- 每条 `sources` 记录中的 `document_url` 和 `preview_url`。
- `diagnosis`：故障码、安全等级、证据充足度、检查清单和需要继续确认的问题。
- `image_recognition`：用户上传图片参与本次检索时的本地 OCR 结果。

诊断追问会保存到 `conversations.pending_question`。用户在同一 `conversation_id` 下回答“是、否、没有、亮了、不确定”等短句时，服务端会自动把它解释为对上一条诊断追问的回答，而不是创建一个无上下文的新问题。响应中的 `conversation_context.continued` 可用于确认是否成功承接上一轮。

智能体回答气泡会直接显示相关图片和来源页码。点击PDF图片或“定位原文”会通过 `#page=N` 直接跳到命中页；PPTX点击后显示命中幻灯片预览。网页右侧“相关资料”页签仍可查看完整资料列表。预览文件缓存在 `output/previews`；原始资料不会被复制或修改，文件访问被限制在配置的语料根目录内。

## 性能与运行状态

`config.json` 的 `retrieval` 节控制本地混合检索：

- `candidate_limit`：全文初筛候选数，当前为 240。
- `semantic_limit`：Qdrant 中文语义召回候选数，当前为 80。
- `embedding_model`：本地向量模型，当前为 `BAAI/bge-small-zh-v1.5`。
- `rrf_k`：关键词与语义结果的 RRF 融合参数。
- `cache_max_entries`：相同问题检索结果的最大缓存条数。
- `cache_ttl_seconds`：检索缓存有效时间，当前为 10 分钟。
- `performance_sample_size`：内存中保留的性能样本数。
- `preview_warm_workers`：命中页面图片的后台预热并发数。

可通过以下接口观察状态：

- `GET /api/health`：数据库、豆包配置和检索后端状态。
- `GET /api/performance`：检索、智能体和总响应时间的平均值、P50、P95、最大值及缓存命中率。
- `GET /api/triples?q=刹车片`：使用图谱 FTS5 索引查询相关三元组。
- `GET /api/quality`：构建状态、OCR 覆盖率和纯图片资料统计。
- `GET /api/fault-code?q=P312700`：文档索引与知识图谱双通道故障码查询。
- `POST /api/image/recognize`：识别不超过 8MB 的车辆、仪表或故障码图片。

图片识别接口接收 JSON：

```json
{
  "file_name": "仪表照片.jpg",
  "image_base64": "图片的Base64内容"
}
```

识别后将返回的 `text` 作为 `/api/search` 的 `image_ocr_text` 传入，即可让图片文字与用户问题共同检索企业资料。原图不写入知识库。

每次响应包含 `timing` 和 `retrieval.timing_ms`。2026-08-04 本机回归测试中，Qdrant 与 FTS5 冷查询约 220ms，缓存命中约 0.6ms；快速模式约 0.9 秒开始显示文字、约 3 秒完成，深度模式受 Kimi K3 推理速度影响，首字和完整回答会明显更慢。

数据量继续增长时的演进路线见 [SCALING.md](SCALING.md)。

## 已实现的文字版要求

- 车系与用/养/修/保分库。
- 文档解析、启用状态、版本替换和来源定位。
- 全文召回、字符向量召回、重排和图谱邻接扩展。
- 三元组 FTS5 索引、检索结果缓存、分阶段耗时统计和页面预览后台预热。
- 扫描图纸本地 OCR、拍照识别、故障码双通道查询和引导式诊断。
- 维修同义词扩展、场景软优先和多文档来源分散，避免相关资料被场景硬过滤或单本文档垄断。
- 专有知识答案与引用源分离展示。
- 命中文档原文打开、PDF页和PPT幻灯片图片预览。
- 不少于 5 轮会话记忆的数据结构。
- 赞/踩纠偏记录及管理员导出所需字段。
- 标准实体表、关系表和三元组 CSV。

图片识别、语音输入、VIN 静态字段平台接入和通用大模型生成属于外部模型或业务系统集成项，本地版本提供对应接口字段，不伪造这些能力。

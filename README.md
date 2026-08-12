# 中重卡智能知识库与多模态 RAG 问答系统

这是一个面向商用车用车、保养、维修、保用和故障诊断场景的本地桌面端知识库项目。系统解析企业文档，建立关键词索引、向量索引、任务分类和知识图谱，再由大模型根据检索证据生成带来源的回答。

项目采用“检索模型与回答模型分离”的设计：

- `doubao-embedding-vision` 负责将文字和图片转换为向量。
- Qdrant 负责相似度搜索和任务标签过滤。
- SQLite FTS5 负责关键词、故障码和元数据检索。
- 豆包快速模型和 DeepSeek 深度模型负责阅读召回资料、推理和组织答案。

大模型不直接读取向量数字。后端先从 Qdrant 找到相关资料，再把资料正文、来源和用户问题一起交给回答模型，这就是检索增强生成（RAG）。

## 工作原理

```text
PDF / Word / Excel / PPT / 图片
              ↓
      文档解析、OCR、切片
              ↓
       元数据与任务分类
              ↓
┌─────────────┼──────────────────┐
│             │                  │
文字向量       PDF页面视觉向量      独立图片视觉向量
│             │                  │
└─────────────┼──────────────────┘
              ↓
任务路由 + FTS5 + Qdrant + 知识图谱
              ↓
      相关正文、文件和页码
              ↓
     快速回答或深度诊断模型
              ↓
  答案、引用资料、原文位置和追问
```

## 核心设计

### 多模态向量索引

系统将不同类型的数据保存在独立集合中：

- `truck_knowledge_chunks_doubao_vision`：文档文字切片。
- `truck_knowledge_pdf_pages_doubao_vision`：PDF 页面渲染图。
- `truck_knowledge_images_doubao_vision`：独立图片文件。

文字问题会被同一向量模型转换成查询向量，再与已构建的知识向量计算相似度。图片查询和 PDF 页面检索可以使用视觉向量集合，不需要把图片内容强行转换成普通文本。

### 任务路由

系统先识别问题类型，再选择对应知识分区，减少不相关资料参与排序：

| 任务类型 | 用途 |
|---|---|
| `vin` | VIN和车辆静态信息精确查询 |
| `fault_code` | P码、SPN、FMI和控制器故障码 |
| `symptom_diagnosis` | 异响、动力不足、无法启动等症状诊断 |
| `usage` | 用车与驾驶操作知识 |
| `maintenance` | 保养项目、周期和油液规格 |
| `warranty` | 保用、保修和索赔规则 |
| `service_technical` | 服务技术文件和技术通知 |
| `drawing` | 电路图、原理图和维修图纸 |
| `claim_case` | 维修及索赔案例 |
| `general` | 通用资料 |

### 故障码精确检索

SPN、FMI和P码不只依赖语义向量。`build_task_index.py` 会从资料中抽取结构化故障码，查询时优先进行精确匹配。

例如输入 `SPN1172` 时，系统可以聚合同一 SPN 下不同 FMI 的定义。精确命中后无需扫描整个向量库，从而避免把故障码数字误识别为里程、订单号或 VIN 片段。

### 混合召回

普通专业问题使用多路召回：

1. SQLite FTS5 关键词匹配。
2. Qdrant 语义向量搜索。
3. 任务类型和车辆元数据过滤。
4. RRF 融合排序。
5. 知识图谱实体关系扩展。
6. 来源分散，避免单本文档垄断结果。

### 回答模型

快速和深度模式共享同一检索层，区别在答案生成阶段：

- 快速模式：适合日常知识查询，使用轻量回答模型。
- 深度模式：适合复杂故障诊断，使用 DeepSeek 阅读更多证据并组织检查步骤。

当前桌面端默认使用豆包快速模型 `doubao-seed-2-0-lite-260215`，深度模式使用 `deepseek-v4-pro-260425`。两个回答模型共用同一个方舟 API Key，但模型名称和回答策略不同。回答模型可以替换，而不必重新生成知识库向量；向量是否需要重建取决于向量模型、维度和切片内容，而不是回答模型。

## 代码结构

| 文件 | 作用 |
|---|---|
| `scripts/build_kb.py` | 解析资料、OCR、切片并建立 SQLite 索引 |
| `scripts/import_vin_data.py` | 导入 VIN 主数据并生成 VIN 分类检索切片 |
| `scripts/build_task_index.py` | 建立任务分类和结构化故障码索引 |
| `scripts/task_router.py` | 识别查询任务并执行精确路由 |
| `scripts/doubao_vision_store.py` | 建立文字多模态向量集合 |
| `scripts/doubao_pdf_page_store.py` | 渲染 PDF 页面并建立视觉向量 |
| `scripts/doubao_image_store.py` | 为独立图片建立视觉向量 |
| `scripts/apply_task_metadata.py` | 给已有 Qdrant 向量补充任务标签 |
| `scripts/query_kb.py` | 关键词、向量、任务分类和图谱混合检索 |
| `scripts/serve_doubao_vision.py` | 多模态向量问答服务入口 |
| `scripts/serve.py` | 本地 BGE 兼容服务入口 |
| `scripts/doubao_client.py` | 火山方舟回答模型适配器 |
| `scripts/diagnosis.py` | 故障诊断、追问和安全提示 |
| `scripts/source_preview.py` | 文档、PDF 页和幻灯片预览 |
| `schema.sql` | 文档、图谱、会话、反馈、意图和 VIN 数据结构 |
| `web/` | 桌面端和移动端页面 |
| `wechat_miniapp/` | 可导入微信开发者工具的原生小程序工程 |

## 环境配置

复制配置模板：

```powershell
Copy-Item .\.env.example .\.env
```

在 `.env` 中配置自己的服务参数（`.env` 只保存在本地，不提交到 GitHub）：

```env
ARK_API_KEY=你的方舟API密钥
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_FAST_MODEL=doubao-seed-2-0-lite-260215
ARK_DEEP_MODEL=deepseek-v4-pro-260425

DOUBAO_EMBEDDING_API_KEY=你的向量模型密钥
DOUBAO_EMBEDDING_URL=https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal
DOUBAO_EMBEDDING_MODEL=doubao-embedding-vision-250615
DOUBAO_EMBEDDING_DIMENSIONS=2048
```

真实密钥只能保存在本地 `.env` 中，不能写进前端或提交到 GitHub。

## 构建知识库

### 1. 解析文档

```powershell
python .\scripts\build_kb.py
```

### 2. 导入 VIN 主数据（可选）

VIN CSV 保存在源码目录之外，默认字段包括 `car_vin`、车型、车系、发动机、变速箱、排放、版本和下线时间。导入器会校验 17 位 VIN、合并完全重复记录、写入精确查询表，并按车系和车辆类型生成可检索切片：

```powershell
python .\scripts\import_vin_data.py --csv "你的VIN文件.csv" --batch-size 10
```

完整 VIN 通过 SQLite 主键精确查询；向量切片用于按车型、发动机、变速箱和版本等条件进行语义检索。原始 VIN 文件、数据库和向量集合均不会提交到 GitHub。

### 3. 建立任务分类与故障码索引

```powershell
python .\scripts\build_task_index.py
```

### 4. 建立文字向量

```powershell
python .\scripts\doubao_vision_store.py --workers 1 --batch-size 8 --timeout 90
```

### 5. 建立 PDF 页面和图片向量

```powershell
python .\scripts\doubao_pdf_page_store.py --workers 1 --batch-size 2
python .\scripts\doubao_image_store.py --workers 1 --batch-size 4
```

向量构建脚本支持增量处理和中断续跑。已经存在的唯一 ID 会被跳过，只处理新增或缺失内容。低并发参数可减少云端向量接口的限流风险。

### 6. 同步任务标签

```powershell
python .\scripts\apply_task_metadata.py `
  --path output\qdrant_doubao_vision `
  --collection truck_knowledge_chunks_doubao_vision
```

该操作只更新 Qdrant payload，不重新调用向量 API。

## 启动服务

```powershell
.\scripts\start_vectorized.ps1
```

或者：

```powershell
python .\scripts\serve_doubao_vision.py
```

- 桌面端：<http://127.0.0.1:8009/>
- 移动端：<http://127.0.0.1:8009/mini/>
- 健康检查：<http://127.0.0.1:8009/api/health>

当前桌面端启动的是豆包多模态向量版本：回答前先把用户问题转换为查询向量，并与 Qdrant 中的文字、PDF 页面和图片向量进行混合检索，再把召回正文和来源交给回答模型。桌面端还支持快速/深度模式、NDJSON 流式输出、连续追问、故障码精确检索、原文页码定位、知识图谱关系、反馈记录和 Excel 批量诊断。

批量诊断功能通过以下接口工作：

- `POST /api/batch/import`：导入车系和客服问题 Excel，保留原始问题并生成工程师口吻问题。
- `POST /api/batch/diagnose`：使用工程师口吻问题检索知识库，输出一条原因对应一条维修方案。
- `POST /api/batch/export`：将批量原因、维修方案、验证方法、来源和处理状态导出为 Excel。

客服记录不会被强制归并到固定类别。系统只会先去除来电记录等无关表述，将问题改写为“如何排查和处理”的工程师提问，再进行检索和回答；检索不到相关企业资料时返回“知识库无相关知识”。

微信小程序工程位于 `wechat_miniapp/`，开发者工具预览时默认连接
`http://127.0.0.1:8009`。真机调试需在设置页填写运行后端电脑的局域网地址；正式发布时需改用 HTTPS，并在微信公众平台配置合法域名。小程序只保存后端地址，不保存任何模型密钥。

本地 BGE 兼容版本可通过以下命令运行：

```powershell
python .\scripts\serve.py
```

默认地址为 <http://127.0.0.1:8008/>。

## 主要接口

- `POST /api/search`：普通问答。
- `POST /api/search/stream`：NDJSON 流式问答。
- `POST /api/agent/chat`：基于知识证据调用回答模型。
- `GET /api/health`：模型和检索后端状态。
- `GET /api/documents`：文档列表。
- `GET /api/triples`：知识图谱查询。
- `GET /api/fault-code`：故障码查询。
- `POST /api/image/recognize`：图片文字识别。
- `POST /api/feedback`：回答反馈与纠偏。

## 数据安全

- 服务默认只监听本机 `127.0.0.1`。
- `.env`、`output/`、日志、模型缓存和本地数据库均被 `.gitignore` 排除。
- 原始文档、业务 VIN 数据和构建后的向量集合不会随源码上传。
- GitHub 仓库只保存项目代码、配置模板和技术说明。

多模态向量细节见 [DOUBAO_VISION.md](DOUBAO_VISION.md)，大规模部署建议见 [SCALING.md](SCALING.md)。

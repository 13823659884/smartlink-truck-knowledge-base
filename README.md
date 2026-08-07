# 鱼快创领中重卡智能知识库（豆包多模态向量版）

本项目是面向中重卡、轻卡用车、保养、维修、保用与故障诊断场景的本地 RAG 问答系统。当前主版本使用火山方舟 `doubao-embedding-vision` 对文字、PDF 页面和独立图片分别建立 2,048 维向量索引，并通过任务分类、关键词检索、语义检索、知识图谱与大模型生成组合回答。

原有 BGE 向量库和 8008 服务仍保留作为兼容回退，不会被豆包向量库覆盖。

## 当前处理规模

截至 2026-08-07，本地完整数据处理结果如下：

| 数据类型 | 已完成 | 状态 |
|---|---:|---|
| 启用知识切片 | 95,252 | 已完成 |
| 豆包文字向量 | 95,252 | 已完成，2,048 维 |
| PDF 文件 | 210 | 已完成 |
| PDF 页面向量 | 21,840 | 已完成，2,048 维 |
| 独立图片向量 | 312 | 已完成，2,048 维 |
| 任务分类标签 | 95,252 | 已完成 |
| 结构化 SPN/FMI 记录 | 1,234 | 已完成 |
| 最终失败项 | 0 | 全部补跑成功 |

生成后的数据库、向量集合、原始资料、日志和 API Key 不上传 GitHub。克隆项目后需要准备自己的资料和模型密钥重新构建，或挂载已有的 `output/` 目录。

## 系统流程

```text
PDF / Word / Excel / PPT / 图片
              ↓
      文档解析、OCR、切片
              ↓
    任务分类 + SPN/FMI结构化
              ↓
┌─────────────┼──────────────────┐
│             │                  │
文字向量       PDF页面视觉向量      独立图片视觉向量
95,252         21,840             312
│             │                  │
└─────────────┼──────────────────┘
              ↓
任务路由 → 精确查询 / 分类向量检索 / FTS5 / 图谱
              ↓
    豆包快速问答或 Kimi K3 深度诊断
              ↓
答案 + 分段引用 + 原文页码 + 连续追问
```

## 任务分类

系统不会把全部资料混在一起盲目召回，而是先识别用户任务，再进入对应知识分区：

| 任务类型 | 用途 |
|---|---|
| `vin` | VIN 车辆静态信息精确查询 |
| `fault_code` | P码、SPN、FMI 和控制器故障码查询 |
| `symptom_diagnosis` | 异响、动力不足、无法启动等症状诊断 |
| `usage` | 用车与驾驶操作知识 |
| `maintenance` | 保养项目、周期与油液规格 |
| `warranty` | 保用、保修与索赔规则 |
| `service_technical` | 服务技术文件和技术通知 |
| `drawing` | 电路图、原理图和维修图纸 |
| `claim_case` | 维修及索赔案例，仅作为补充证据 |
| `general` | 未归入专用任务的通用资料 |

故障码采用专用结构化索引。例如只输入 `SPN1172` 时，系统直接返回其 FMI 2、3、4、10 等全部已知定义，不再让数字与里程、VIN片段或索赔单号混搜。精确命中时会跳过全库向量检索。

## 主要目录

- `scripts/build_kb.py`：解析资料并建立 SQLite、FTS5、图谱和原兼容索引。
- `scripts/build_task_index.py`：为全部切片建立任务分类，并抽取 SPN/FMI 字典。
- `scripts/task_router.py`：识别查询任务，执行故障码精确路由。
- `scripts/doubao_vision_store.py`：建立豆包文字镜像向量库。
- `scripts/doubao_pdf_page_store.py`：将每个 PDF 页面渲染后建立视觉向量。
- `scripts/doubao_image_store.py`：为独立图片建立视觉向量。
- `scripts/apply_task_metadata.py`：不重新计算向量，直接给 Qdrant 补充任务标签。
- `scripts/query_kb.py`：任务路由、FTS5、Qdrant、RRF 和图谱混合检索。
- `scripts/serve_doubao_vision.py`：当前主版本问答服务，默认端口 8009。
- `scripts/serve.py`：原 BGE 兼容服务，默认端口 8008。
- `web/`：桌面端和移动端 Web 界面。
- `schema.sql`：知识库、会话、反馈、意图和 VIN 表结构。

## 环境配置

复制示例配置：

```powershell
Copy-Item .\.env.example .\.env
```

在本地 `.env` 中填写自己的火山方舟密钥：

```env
ARK_API_KEY=你的方舟API密钥
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
ARK_FAST_MODEL=doubao-seed-2-0-lite-260215
ARK_DEEP_MODEL=kimi-k3

DOUBAO_EMBEDDING_API_KEY=你的向量模型密钥
DOUBAO_EMBEDDING_URL=https://ark.cn-beijing.volces.com/api/plan/v3/embeddings/multimodal
DOUBAO_EMBEDDING_MODEL=doubao-embedding-vision
DOUBAO_EMBEDDING_DIMENSIONS=2048
```

`.env` 已加入 `.gitignore`，禁止把真实密钥写入源码、小程序或 GitHub。

## 全量构建

### 1. 解析资料与生成切片

```powershell
python .\scripts\build_kb.py
```

### 2. 建立任务分类和故障码精确索引

```powershell
python .\scripts\build_task_index.py
```

### 3. 建立豆包文字向量

```powershell
python .\scripts\doubao_vision_store.py --workers 1 --batch-size 8 --timeout 90
```

### 4. 建立 PDF 页面和图片向量

```powershell
python .\scripts\doubao_pdf_page_store.py --workers 1 --batch-size 2
python .\scripts\doubao_image_store.py --workers 1 --batch-size 4
```

使用单并发是为了降低火山方舟 HTTP 429 限流风险。所有脚本均按唯一 ID 增量写入，任务中断后重复运行即可补齐缺失项，不会重复生成已有向量。

### 5. 将任务标签同步到向量集合

```powershell
python .\scripts\apply_task_metadata.py `
  --path output\qdrant_doubao_vision `
  --collection truck_knowledge_chunks_doubao_vision

python .\scripts\apply_task_metadata.py `
  --path output\qdrant_doubao_pdf_pages `
  --collection truck_knowledge_pdf_pages_doubao_vision
```

该步骤只更新 Qdrant payload，不重新调用向量 API。

## 启动当前向量化版本

推荐使用：

```powershell
.\scripts\start_vectorized.ps1
```

也可以直接运行：

```powershell
python .\scripts\serve_doubao_vision.py
```

- 桌面端：<http://127.0.0.1:8009/>
- 移动端：<http://127.0.0.1:8009/mini/>
- 健康检查：<http://127.0.0.1:8009/api/health>

服务会先启动页面，再在后台预热本地 2,048 维 Qdrant 集合。健康接口中的 `retrieval.qdrant.ready=true` 表示向量模型已经可以查询。

## 原版兼容服务

如需对比原 BGE 版本：

```powershell
python .\scripts\serve.py
```

访问 <http://127.0.0.1:8008/>。该版本使用 `BAAI/bge-small-zh-v1.5` 和 `truck_knowledge_chunks`，不会影响豆包向量集合。

## 问答能力

- 豆包快速模式与 Kimi K3 深度模式。
- NDJSON 流式回答与阶段状态。
- 连续追问、待确认问题和会话上下文。
- 故障码精确反查及全部 FMI 变体聚合。
- SQLite 关键词、Qdrant 语义、任务标签与图谱混合召回。
- 参考资料、文件位置、PDF 页码和原文链接。
- VIN 精确数据表及车辆字段接口。
- 点赞、点踩、纠偏和反馈导出。
- 桌面端与移动端共用同一知识库和接口。

## 数据安全

- 服务默认只监听 `127.0.0.1`。
- API Key 仅从本地 `.env` 读取。
- 原始文档、VIN 数据、SQLite 和 Qdrant 均位于被忽略的 `output/` 或外部资料目录。
- GitHub 只保存可复现源码与示例配置。
- 专业问题要求模型依据召回资料作答；通用问题可由大模型自主回答。

更多多模态构建细节见 [DOUBAO_VISION.md](DOUBAO_VISION.md)，数据继续增长后的部署建议见 [SCALING.md](SCALING.md)。

# 功能模块说明

| 功能模块 | 代码实现 | 说明 |
|---|---|---|
| 文档解析 | `scripts/build_kb.py` | 解析PDF、Word、Excel、PPT并保存来源定位 |
| OCR | `scripts/ocr_engine.py` | 识别扫描文档和用户上传图片中的文字 |
| 文本切片 | `scripts/build_kb.py` | 按长度和重叠窗口生成可引用知识片段 |
| 任务分类 | `scripts/build_task_index.py` | 为切片标记查询任务类型 |
| 故障码字典 | `scripts/build_task_index.py` | 抽取SPN、FMI、描述、等级和来源 |
| 查询路由 | `scripts/task_router.py` | VIN与故障码走精确查询，其他问题走混合检索 |
| 文字向量 | `scripts/doubao_vision_store.py` | 使用多模态向量模型处理文字切片 |
| PDF页面向量 | `scripts/doubao_pdf_page_store.py` | 将PDF页面渲染为图片后建立视觉向量 |
| 图片向量 | `scripts/doubao_image_store.py` | 为独立图片建立视觉向量集合 |
| 任务标签同步 | `scripts/apply_task_metadata.py` | 更新Qdrant payload而不重算向量 |
| 混合检索 | `scripts/query_kb.py` | 融合FTS5、Qdrant、任务标签、RRF和图谱 |
| 知识图谱 | `schema.sql`、`scripts/query_kb.py` | 保存实体、关系、三元组和证据来源 |
| 快速回答 | `scripts/doubao_client.py` | 使用精简证据生成日常回答 |
| 深度诊断 | `scripts/doubao_client.py` | 将更多检索证据交给深度回答模型 |
| 流式输出 | `scripts/serve.py` | 返回状态、增量文本和最终结果 |
| 连续追问 | `scripts/serve.py`、`scripts/diagnosis.py` | 保存会话主题、待确认问题和上下文 |
| 原文定位 | `scripts/source_preview.py` | 提供文件、PDF页和幻灯片预览 |
| VIN接口 | `schema.sql`、`scripts/serve.py` | 保存车辆静态字段并支持精确查询 |
| 反馈纠偏 | `schema.sql`、`scripts/serve.py` | 保存点赞、点踩、意见和导出数据 |
| 桌面与移动界面 | `web/` | 共用同一问答、会话和资料接口 |

## 检索原则

1. 编号类数据优先精确查询。
2. 专业问题先检索企业资料，再调用回答模型。
3. 向量结果必须保留来源文件和位置。
4. 任务类型用于过滤和重排，不替代原始证据。
5. 回答模型可以更换，知识向量与回答模型相互独立。

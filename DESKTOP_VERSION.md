# 桌面端当前版本说明

## 运行入口

桌面端使用 `scripts/start_vectorized.ps1` 启动，默认地址为 `http://127.0.0.1:8009/`。入口会启动 `scripts/serve_doubao_vision.py`，加载 SQLite 知识库、Qdrant 豆包多模态向量集合和本地知识图谱。

## 当前配置

配置模板为 `.env.example`，实际运行时复制为本地 `.env` 并填写自己的 API Key。当前版本使用：

- 快速回答：`doubao-seed-2-0-lite-260215`
- 深度回答：复用快速模型 `doubao-seed-2-0-lite-260215`，但启用深度诊断策略
- 多模态向量：`doubao-embedding-vision-250615`
- 方舟地址：`https://ark.cn-beijing.volces.com/api/v3`
- 向量套餐接口：`https://ark.cn-beijing.volces.com/api/plan/v3/embeddings/multimodal`
- 向量维度：2048
- 向量集合：`truck_knowledge_chunks_doubao_vision`
- PDF 页面集合：`truck_knowledge_pdf_pages_doubao_vision`
- 图片集合：`truck_knowledge_images_doubao_vision`

当前通过 `ARK_DEEP_USE_FAST=true` 让快速回答和深度诊断复用 `ARK_FAST_API_KEY`；向量接口仍独立使用 `DOUBAO_EMBEDDING_API_KEY`。密钥不写入前端、配置模板或 GitHub。

## 桌面端能力

桌面端先对问题进行任务识别和混合检索，再调用回答模型。支持文字切片、PDF 页面和图片向量检索、故障码精确匹配、连续追问、流式输出、知识图谱关系、原文定位、参考资料和反馈记录。

车辆专业问题支持两阶段引导。信息不完整时，第一轮确认问题分类、车辆系列、能源类型和回答重点；第二轮才检索并生成定向答案。用户可选择含义与标准、可能原因、排查处理、注意事项或完整诊断。问题已经明确时直接回答，VIN命中本地车辆档案时自动预填车辆信息。

桌面端支持单份资料增量导入。系统自动保存文件、解析内容、生成切片、识别任务分类、更新 SQLite 索引并写入现有文字向量集合，不重建全部知识库。首次构建、大批量资料或 PDF 页面和独立图片视觉向量仍使用离线构建脚本。

批量诊断支持导入车系与客服问题 Excel。系统保留客服原始问题，同时把“客户来电、咨询、报修”等记录改写为工程师口吻，再基于改写后的问题检索和回答；不会把示例问题强制归并成固定类别。结果包含原因、对应维修方案、验证方法和参考资料，支持导出 Excel。资料不足时返回“知识库无相关知识”。

## 启动前准备

```powershell
Copy-Item .env.example .env
python scripts/build_kb.py
python scripts/build_task_index.py
.\scripts\start_vectorized.ps1
```

`output/` 中的数据库、向量库、原始资料和日志属于本地运行数据，不随桌面端源码提交。

# 项目源码说明

## 项目目标

本项目将车辆维修文档转换成可检索的结构化知识，并通过RAG方式为大模型提供企业证据。系统重点解决以下问题：

- 非结构化文档难以统一查询。
- 纯关键词搜索无法理解同义表达。
- 纯向量搜索容易混淆故障码、编号和业务流水号。
- 大模型直接回答专业问题容易缺少来源。
- PDF页面、图纸和图片资料难以参与统一召回。

## 模块划分

- 文档层：解析PDF、Word、Excel、PPT和图片，保留来源位置。
- 索引层：SQLite FTS5、Qdrant多模态向量和知识图谱。
- 路由层：识别VIN、故障码、诊断、保养等任务类型。
- 检索层：关键词、语义、元数据和图谱混合召回。
- 生成层：将召回证据交给快速或深度回答模型。
- 应用层：桌面端、移动端、会话、反馈和原文跳转。

## 主要入口

```powershell
python .\scripts\build_kb.py
python .\scripts\import_vin_data.py --csv "你的VIN文件.csv"
python .\scripts\build_task_index.py
python .\scripts\doubao_vision_store.py
.\scripts\start_vectorized.ps1
```

## 源码与数据边界

仓库保存可复现源码、配置模板和说明文档。以下内容属于部署环境，不纳入源码：

- 原始企业资料和VIN业务数据。
- SQLite知识库和Qdrant向量集合。
- 文档解析缓存、OCR缓存和预览文件。
- 日志、模型文件和API密钥。

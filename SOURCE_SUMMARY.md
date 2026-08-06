# 原版中重卡知识库源码汇总

## 功能范围

- 文档解析：PDF、DOC/DOCX、XLS/XLSX、PPTX，支持内容哈希缓存和扫描件 OCR。
- 知识检索：SQLite FTS5 关键词检索、Qdrant 中文语义检索、RRF 混合排序。
- 知识图谱：实体、关系和标准三元组查询。
- 问答路由：车辆用/养/修/保专业问题严格依据企业知识库；闲聊和通用问题交给通用模型回答。
- 连续诊断：会话上下文、待确认问题、故障码结构化输出和安全提示。
- 引用溯源：回答中的资料编号、原始文档定位和 PDF 页码跳转。
- 前端：桌面端 Web 问答界面、手机风格 Web 演示界面，以及可复用的后端接口。
- 管理接口：知识库统计、历史记录、反馈纠偏、意图配置和 VIN 主数据接口。

## 目录说明

- `scripts/`：构建、检索、Qdrant、OCR、问答适配器和本地服务。
- `web/`：桌面端和移动端 Web 页面。
- `config.json`：车系、场景、切片和检索配置。
- `schema.sql`：知识库、图谱、会话、反馈、意图和 VIN 表结构。
- `requirements.txt`：可复现安装依赖。
- `SOURCE_SUMMARY.md`：源码边界与部署说明。

## 不上传的本地文件

原始资料、`output/` 构建产物、Qdrant 向量、解析缓存、OCR 模型、日志和 `.env` 密钥均已加入 `.gitignore`。部署时需自行准备资料目录，并从 `.env.example` 创建 `.env`。

## 启动方式

```powershell
python .\scripts\build_kb.py
python .\scripts\serve.py
```

服务默认地址：`http://127.0.0.1:8008/`

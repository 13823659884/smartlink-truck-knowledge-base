# 豆包多模态镜像向量库

该版本在原有 BGE/Qdrant 知识库之外，单独创建 `truck_knowledge_chunks_doubao_vision` 集合，使用 `doubao-embedding-vision` 生成 2048 维向量。原集合、SQLite 切片和原版问答服务不会被覆盖。

## 构建镜像向量

先在本地 `.env` 中配置 `DOUBAO_EMBEDDING_API_KEY`（也可临时使用已有 `ARK_API_KEY`），然后运行：

```powershell
python scripts/doubao_vision_store.py --workers 4 --batch-size 16
```

脚本会按 chunk ID 增量写入，支持中断后重复运行；已完成的片段会跳过。报告写入 `output/qdrant_doubao_vision_report.json`，日志由启动器或终端保存。

## 启动独立问答版本

```powershell
python scripts/serve_doubao_vision.py
```

默认地址为 `http://127.0.0.1:8009`。它复用原桌面端和小程序端界面、连续追问和答案模型，只把语义检索切换到豆包镜像集合；原版服务仍使用 8008 和原 BGE 集合。

## 环境变量

```text
DOUBAO_EMBEDDING_API_KEY=...
DOUBAO_EMBEDDING_URL=https://ark.cn-beijing.volces.com/api/plan/v3/embeddings/multimodal
DOUBAO_EMBEDDING_MODEL=doubao-embedding-vision
DOUBAO_EMBEDDING_DIMENSIONS=2048
DOUBAO_VISION_QDRANT_PATH=output/qdrant_doubao_vision
DOUBAO_VISION_QDRANT_COLLECTION=truck_knowledge_chunks_doubao_vision
```

不要把 `.env` 或 API Key 提交到 GitHub。原始图片可单独建立图片集合：

```powershell
python scripts/doubao_image_store.py --workers 4 --batch-size 8
```

图片集合为 `truck_knowledge_images_doubao_vision`，与文字镜像集合和原版集合完全分离。脚本会扫描知识库目录下的 PNG/JPG/JPEG/WEBP/BMP/GIF 文件并支持断点续跑。

PDF 页面也可全部渲染为图像并向量化（当前资料共 210 个 PDF、21,840 页）：

```powershell
python scripts/doubao_pdf_page_store.py --workers 4 --batch-size 4
```

PDF 页面单独写入 `truck_knowledge_pdf_pages_doubao_vision` 集合，任务支持断点续跑。

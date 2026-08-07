"""独立的豆包多模态向量检索问答服务。

该入口复用原版桌面端/小程序端界面和问答逻辑，只替换语义检索集合；
原版服务仍使用原来的 BGE/Qdrant 集合，互不覆盖。
"""

from __future__ import annotations

import os
import sys
import threading

os.environ.setdefault("DOUBAO_VISION_MODE", "1")

import doubao_vision_store
import query_kb
import serve


# query_kb 只在该独立进程内切换语义检索函数，不影响原版服务进程。
query_kb.semantic_search = doubao_vision_store.semantic_search
serve.semantic_search = doubao_vision_store.semantic_search
serve.PORT = int(os.getenv("DOUBAO_VISION_PORT", "8009"))

_VECTOR_STATE = {"ready": False, "loading": True, "error": "", "points": 0}


def vector_status() -> dict[str, object]:
    if not _VECTOR_STATE["ready"]:
        config = doubao_vision_store.vision_config()
        return {
            "ready": False,
            "loading": bool(_VECTOR_STATE["loading"]),
            "error": str(_VECTOR_STATE["error"]),
            "path": config["path"],
            "collection": config["collection"],
            "model": config["model"],
            "dimensions": config["dimensions"],
            "points": int(_VECTOR_STATE["points"]),
        }
    return doubao_vision_store.status()


serve.qdrant_status = vector_status


def warm_vector_runtime() -> None:
    try:
        serve.warm_chunk_cache(serve.DATABASE_PATH)
        status = doubao_vision_store.status()
        _VECTOR_STATE["ready"] = bool(status.get("ready"))
        _VECTOR_STATE["error"] = str(status.get("error", ""))
    except Exception as exc:
        _VECTOR_STATE["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _VECTOR_STATE["loading"] = False


def main() -> int:
    if not serve.DATABASE_PATH.exists():
        print("知识库尚未构建，请先运行 scripts/build_kb.py", file=sys.stderr)
        return 2
    serve.ensure_runtime_schema()
    with serve.connect() as connection:
        _VECTOR_STATE["points"] = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.enabled = 1
                """
            ).fetchone()[0]
        )
    server = serve.ThreadingHTTPServer((serve.HOST, serve.PORT), serve.KnowledgeHandler)
    threading.Thread(
        target=warm_vector_runtime,
        name="doubao-vector-warmup",
        daemon=True,
    ).start()
    print(f"豆包向量知识库服务已启动：http://{serve.HOST}:{serve.PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath 'output\knowledge_base.db')) {
    throw '尚未找到 output\knowledge_base.db，请先运行 python scripts\build_kb.py'
}

if (-not (Test-Path -LiteralPath 'output\task_index.db')) {
    Write-Host '正在建立任务分类与故障码精确索引...'
    python scripts\build_task_index.py
    if ($LASTEXITCODE -ne 0) {
        throw '任务分类索引构建失败'
    }
}

Write-Host '启动豆包多模态向量知识库：http://127.0.0.1:8009/'
python scripts\serve_doubao_vision.py

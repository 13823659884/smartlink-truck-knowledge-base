$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath 'output\knowledge_base.db')) {
    throw 'Missing output\knowledge_base.db. Run python scripts\build_kb.py first.'
}

if (-not (Test-Path -LiteralPath 'output\task_index.db')) {
    Write-Host 'Building task and fault-code indexes...'
    python scripts\build_task_index.py
    if ($LASTEXITCODE -ne 0) {
        throw 'Task index build failed.'
    }
}

Write-Host 'Starting multimodal vector service: http://127.0.0.1:8009/'
python scripts\serve_doubao_vision.py

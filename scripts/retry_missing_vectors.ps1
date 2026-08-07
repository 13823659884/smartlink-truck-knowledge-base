$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$env:PYTHONIOENCODING = 'utf-8'

python scripts\doubao_vision_store.py --workers 1 --batch-size 8 --timeout 90
Start-Sleep -Seconds 15
python scripts\doubao_pdf_page_store.py --workers 1 --batch-size 2

python scripts\apply_task_metadata.py `
    --path output\qdrant_doubao_vision `
    --collection truck_knowledge_chunks_doubao_vision
python scripts\apply_task_metadata.py `
    --path output\qdrant_doubao_pdf_pages `
    --collection truck_knowledge_pdf_pages_doubao_vision

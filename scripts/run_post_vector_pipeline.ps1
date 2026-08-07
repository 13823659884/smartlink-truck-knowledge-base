param(
    [string]$VectorProcessIds = ''
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

foreach ($processId in ($VectorProcessIds -split ',' | Where-Object { $_ })) {
    while (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
        Start-Sleep -Seconds 20
    }
}

$env:PYTHONIOENCODING = 'utf-8'
python scripts\build_task_index.py

# Local Qdrant collections are single-process stores. Stop only the dedicated
# mirror service while payload labels are written, then bring it back online.
$visionServices = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'scripts\\serve_doubao_vision\.py'
}
foreach ($service in $visionServices) {
    Stop-Process -Id $service.ProcessId -Force -ErrorAction SilentlyContinue
}

python scripts\apply_task_metadata.py `
    --path output\qdrant_doubao_vision `
    --collection truck_knowledge_chunks_doubao_vision
python scripts\apply_task_metadata.py `
    --path output\qdrant_doubao_pdf_pages `
    --collection truck_knowledge_pdf_pages_doubao_vision
python scripts\apply_task_metadata.py `
    --path output\qdrant_doubao_images `
    --collection truck_knowledge_images_doubao_vision

$pythonExe = (Get-Command python).Source
Start-Process -FilePath $pythonExe `
    -ArgumentList @('scripts\serve_doubao_vision.py') `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ProjectRoot 'logs\serve_doubao_vision.log') `
    -RedirectStandardError (Join-Path $ProjectRoot 'logs\serve_doubao_vision_error.log')

@{
    completed_at = (Get-Date).ToString('o')
    status = 'complete'
} | ConvertTo-Json | Set-Content -LiteralPath output\post_vector_pipeline_complete.json -Encoding UTF8

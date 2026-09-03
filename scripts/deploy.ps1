# 规智库一键 Docker 部署：构建镜像并启动 Qdrant + 后端 + 前端
# 用法：
#   powershell scripts/deploy.ps1            # 首次部署（构建镜像并启动）
#   powershell scripts/deploy.ps1 -Rebuild   # 修改代码后重新构建
#   powershell scripts/deploy.ps1 -Down      # 停止全部服务
param(
    [switch]$Rebuild,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ComposeFile = Join-Path $Root "docker-compose.yml"

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Host "缺少根目录 .env 配置文件，请先配置再部署。" -ForegroundColor Red
    exit 1
}

if ($Down) {
    Write-Host "停止规智库全部服务..." -ForegroundColor Yellow
    docker compose -f $ComposeFile down
    exit 0
}

# 旧独立 Qdrant 容器与新部署共用 storage/qdrant-server 数据目录，
# 同时运行会导致数据锁冲突，必须先移除（数据不会丢失）。
$legacy = docker ps -aq --filter "name=^rag-qdrant$"
if ($legacy) {
    Write-Host "移除旧的独立 Qdrant 容器（数据保留在 storage/qdrant-server）..." -ForegroundColor Yellow
    docker rm -f rag-qdrant | Out-Null
}

# 端口占用检测：本地开发服务（run.py / uvicorn / npm dev）会与容器端口冲突
$apiPort = (Select-String -Path (Join-Path $Root ".env") -Pattern "^RAG_API_PORT=(\d+)" |
    ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
$webPort = (Select-String -Path (Join-Path $Root ".env") -Pattern "^RAG_FRONTEND_PORT=(\d+)" |
    ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1)
$apiPort = if ($apiPort) { $apiPort } else { "8008" }
$webPort = if ($webPort) { $webPort } else { "3000" }

foreach ($pair in @(@("API", $apiPort), @("前端", $webPort))) {
    $busy = Get-NetTCPConnection -State Listen -LocalPort $pair[1] -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -ne 0 } |
        Where-Object {
            $name = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
            $name -and $name -notmatch '^(docker|com\.docker|wsl|wslrelay|vpnkit)'
        }
    if ($busy) {
        Write-Host ("端口 {0}（{1}）被本地进程占用，请先停止本地开发服务（Ctrl+C 结束 run.py / uvicorn / npm dev），然后重新运行本脚本。" -f $pair[1], $pair[0]) -ForegroundColor Red
        exit 1
    }
}

Write-Host "构建并启动规智库（Qdrant + 后端 API + 前端）..." -ForegroundColor Cyan
$buildArgs = @("compose", "-f", $ComposeFile, "up", "-d", "--build")

# 检测 NVIDIA GPU：有卡才启动本机 embedding/reranker（--profile gpu），
# 无 GPU 的机器自动降级为基础三件套（检索退化为 BM25 词法模式）。
$gpuAvailable = $false
try { $null = & nvidia-smi 2>$null; if ($LASTEXITCODE -eq 0) { $gpuAvailable = $true } } catch {}
if ($gpuAvailable) {
    Write-Host "检测到 NVIDIA GPU：附带启动本机 Embedding/Reranker（首次需下载模型，约数分钟）..." -ForegroundColor Cyan
    $buildArgs += @("--profile", "gpu")
} else {
    Write-Host "未检测到 NVIDIA GPU：跳过本机模型服务（语义检索降级为 BM25 词法模式）。" -ForegroundColor Yellow
    Write-Host "  如需启用，请将 .env 中 RAG_EMBEDDING_BASE_URL / RAG_RERANK_BASE_URL 指向外部推理服务。" -ForegroundColor Yellow
}

if ($Rebuild) { $buildArgs += "--force-recreate" }
docker @buildArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 等待服务就绪
Write-Host "等待服务就绪..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(120)
$apiOk = $false
$webOk = $false
while ((Get-Date) -lt $deadline -and -not ($apiOk -and $webOk)) {
    if (-not $apiOk) {
        try { Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/health" -TimeoutSec 2 | Out-Null; $apiOk = $true } catch {}
    }
    if (-not $webOk) {
        try { Invoke-WebRequest -Uri "http://127.0.0.1:$webPort/" -TimeoutSec 2 -UseBasicParsing | Out-Null; $webOk = $true } catch {}
    }
    if (-not ($apiOk -and $webOk)) { Start-Sleep -Seconds 2 }
}

if ($apiOk -and $webOk) {
    Write-Host ""
    Write-Host "规智库已启动：" -ForegroundColor Green
    Write-Host ("  Web:  http://localhost:{0}" -f $webPort)
    Write-Host ("  API:  http://127.0.0.1:{0}/docs" -f $apiPort)
    Write-Host ""
    Write-Host "常用命令：" -ForegroundColor Green
    Write-Host "  查看日志:  docker compose -f docker-compose.yml logs -f backend"
    Write-Host "  停止服务:  powershell scripts/deploy.ps1 -Down"
    Write-Host "  重新构建:  powershell scripts/deploy.ps1 -Rebuild"
} else {
    Write-Host "部分服务启动超时，请查看日志排查：" -ForegroundColor Yellow
    Write-Host "  docker compose -f docker-compose.yml logs -f"
    exit 1
}

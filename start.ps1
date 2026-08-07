# LearnLove Agent 启动脚本
#
# 用法:
#   .\start.ps1                 交互模式
#   .\start.ps1 --auto          自动监听模式
#   .\start.ps1 --daemon        守护模式
#   .\start.ps1 --config PATH   指定配置
#   .\start.ps1 -h              帮助

param(
    [switch]$auto,
    [switch]$daemon,
    [string]$config,
    [switch]$h,
    [switch]$help
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Python 路径
$venvPython = "$ScriptDir\.venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
}
else {
    $python = "python"
}

# 帮助
if ($h -or $help) {
    Write-Host "用法: start.ps1 [选项]"
    Write-Host ""
    Write-Host "  无参数          交互模式"
    Write-Host "  --auto          自动监听模式"
    Write-Host "  --daemon        守护模式"
    Write-Host "  --config PATH   指定配置文件"
    Write-Host ""
    Write-Host "示例:"
    Write-Host "  .\start.ps1                 交互模式"
    Write-Host "  .\start.ps1 --auto          自动监听"
    Write-Host "  .\start.ps1 --daemon        后台守护"
    return
}

# 标题
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  LearnLove Agent" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# 构建参数
$args = @("-m", "agent.loop")
if ($auto) { $args += "--auto" }
if ($daemon) { $args += "--daemon" }
if ($config) { $args += "--config", $config }

# 启动
& $python $args

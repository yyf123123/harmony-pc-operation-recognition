param([Parameter(Mandatory=$true)][ValidateSet('bilibili','huawei_browser','wps')][string]$App,
      [string]$Python='python', [Parameter(ValueFromRemainingArguments=$true)][string[]]$CollectorArgs)
$ErrorActionPreference='Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try { & $Python -m scripts.collect --app $App @CollectorArgs; exit $LASTEXITCODE }
finally { Pop-Location }

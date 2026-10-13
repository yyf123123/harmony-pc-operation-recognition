param([Parameter(Mandatory=$true)][string]$Sdk,
      [string]$Output='build/collector',[string]$Python='python')
$ErrorActionPreference='Stop'
$sdkPath=(Resolve-Path -LiteralPath $Sdk).Path
$outputPath=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
Push-Location (Split-Path -Parent $PSScriptRoot)
try { & $Python -m collector.build --sdk $sdkPath --output $outputPath; exit $LASTEXITCODE }
finally { Pop-Location }

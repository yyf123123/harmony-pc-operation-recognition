param([Parameter(Mandatory=$true)][string]$App,
      [Parameter(Mandatory=$true)][string]$Data,
      [Parameter(Mandatory=$true)][string]$Output,
      [Parameter(Mandatory=$true)][string]$Schema,
      [string]$Group='trial',[int]$Folds=5,[int]$Seed=20261003,[string]$Python='python')
$ErrorActionPreference='Stop'
$dataPath=(Resolve-Path -LiteralPath $Data).Path
$outputPath=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
Push-Location (Split-Path -Parent $PSScriptRoot)
try { & $Python -m classification.evaluate --app $App --data $dataPath --output $outputPath --schema $Schema --group $Group --folds $Folds --seed $Seed; exit $LASTEXITCODE }
finally { Pop-Location }

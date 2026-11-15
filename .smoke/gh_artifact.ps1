# Download an Actions artifact zip by id and extract to .smoke/gh-logs/.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$artId = $args[0]
$dest = "D:/Code/2026/Browserr/.smoke/gh-logs"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Path $dest -Force | Out-Null

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass" }

$zip = "$dest/artifact-$artId.zip"
Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/actions/artifacts/$artId/zip" -Headers $hdr -OutFile $zip
Write-Output "downloaded: $zip ($((Get-Item $zip).Length) bytes)"
Expand-Archive -Path $zip -DestinationPath $dest -Force
Get-ChildItem $dest -Recurse -File | ForEach-Object { Write-Output "  $($_.FullName.Substring($dest.Length+1)) ($($_.Length) bytes)" }
Write-Output "DONE"

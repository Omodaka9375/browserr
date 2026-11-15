# List releases (including drafts) with their assets.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

$rs = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases?per_page=10" -Headers $hdr
foreach ($r in $rs) {
    Write-Output ("{0}  id={1} draft={2} created={3} assets={4} ({5})" -f $r.tag_name, $r.id, $r.draft, $r.created_at, $r.assets.Count, ($r.assets.name -join ", "))
}
Write-Output "LIST-DONE"

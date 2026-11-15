# Inspect a release by tag: created_at, assets with names/sizes/digests.
$ErrorActionPreference = "Stop"
$repo = "Omodaka9375/browserr"
$tag = $args[0]

$inp = "protocol=https`nhost=github.com`n`n"
$out = $inp | git credential fill 2>$null
$pass = $null
foreach ($line in ($out -split "`n")) {
    if ($line -match "^password=(.+)$") { $pass = $Matches[1] }
}
if (-not $pass) { Write-Output "NO_CREDENTIAL"; exit 1 }
$hdr = @{ "User-Agent" = "kai-ci-check"; "Authorization" = "Bearer $pass";
          "Accept" = "application/vnd.github+json" }

$r = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/tags/$tag" -Headers $hdr
Write-Output "release id=$($r.id) tag=$($r.tag_name) draft=$($r.draft) created=$($r.created_at) author=$($r.author.login)"
Write-Output "body (first 300): $($r.body.Substring(0, [Math]::Min(300, $r.body.Length)))"
Write-Output "assets ($($r.assets.Count)):"
foreach ($a in $r.assets) {
    Write-Output ("  {0}  {1} bytes  digest={2}  id={3}  downloaded={4}" -f $a.name, $a.size, $a.digest, $a.id, $a.download_count)
}
Write-Output "RELEASE-INFO-DONE"

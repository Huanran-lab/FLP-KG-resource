# Start Neo4j Desktop DBMS "11" (Enterprise 2026.06.0).
# Desktop UI often fails when D:\neo4j-community occupies port 7688 (Bolt routing).
$ErrorActionPreference = "Stop"

$db = "C:\Users\huanr\.Neo4jDesktop2\Data\dbmss\dbms-3275a811-495d-4ca4-a25b-c340bf2c4405"
$javaHome = "C:\Users\huanr\.Neo4jDesktop2\Cache\runtime\zulu21.50.19-ca-jre21.0.11-win_x64"
$neo4jBat = Join-Path $db "bin\neo4j.bat"

if (-not (Test-Path $neo4jBat)) {
    Write-Error "DBMS 11 not found at $db"
    exit 1
}
if (-not (Test-Path "$javaHome\bin\java.exe")) {
    Write-Error "Java 21 not found at $javaHome"
    exit 1
}

# Free Bolt routing port 7688 if community Neo4j is holding it
Get-CimInstance Win32_Process -Filter "Name='java.exe'" |
    Where-Object { $_.CommandLine -like '*neo4j-community-5.25.1*' } |
    ForEach-Object {
        Write-Host "Stopping community Neo4j (pid $($_.ProcessId)) to free :7688"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

$env:JAVA_HOME = $javaHome
$env:NEO4J_HOME = $db
$env:NEO4J_CONF = Join-Path $db "conf"
$env:Path = "$javaHome\bin;" + $env:Path

Write-Host "Starting Desktop DBMS 11 ..."
Write-Host "  Browser: http://localhost:7474/"
Write-Host "  Bolt:    bolt://localhost:7687"
Write-Host "  Auth:    neo4j / password"
Set-Location $db
& $neo4jBat console

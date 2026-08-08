$logFile = "C:\Users\Thiago\whatsapp-gemini-bridge\watchdog.log"
$checkIntervalSec = 30

# Ponte (app.py) - reiniciar o processo Python nao afeta a sessao do WhatsApp,
# entao pode ser mais agressivo aqui.
$bridgeFailuresRequired = 2
$bridgeBaseCooldownSec = 30

# Evolution API - reiniciar o container derruba a sessao Baileys (forca reconexao/relogin).
# Ser bem mais conservador aqui e o principal fator para reduzir risco de banimento.
$evolutionFailuresRequired = 3          # ~90s de indisponibilidade sustentada antes de reiniciar
$evolutionBaseCooldownSec = 300         # 5 min minimo entre restarts
$evolutionMaxCooldownSec = 3600         # backoff nunca passa de 1h
$evolutionHealthyResetSec = 900         # 15 min saudavel -> zera o backoff

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "$ts $msg" -Encoding UTF8
}

function Test-Url($url, $timeoutSec) {
    try {
        $resp = Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Restart-Bridge {
    Write-Log "Bridge nao respondeu apos $bridgeFailuresRequired checagens - reiniciando"
    $conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Seconds 2
    Start-Process -FilePath "powershell.exe" -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','C:\Users\Thiago\whatsapp-gemini-bridge\start_bridge.ps1') -WindowStyle Hidden
    Write-Log "Bridge reiniciada"
}

function Restart-EvolutionApi {
    param([int]$CooldownSec)
    Write-Log "Evolution API nao respondeu apos $evolutionFailuresRequired checagens - reiniciando container (proximo restart possivel em ${CooldownSec}s)"
    docker restart evolution_api | Out-Null
    Write-Log "Evolution API reiniciada"
}

Write-Log "Watchdog iniciado"

$lastBridgeRestart = Get-Date "2000-01-01"
$bridgeFailStreak = 0

$lastEvolutionRestart = Get-Date "2000-01-01"
$evolutionFailStreak = 0
$evolutionCurrentCooldownSec = $evolutionBaseCooldownSec
$evolutionLastHealthyAt = Get-Date

while ($true) {
    try {
        $now = Get-Date

        # --- Bridge (app.py) ---
        if (Test-Url "http://127.0.0.1:8000/health" 5) {
            $bridgeFailStreak = 0
        } else {
            $bridgeFailStreak++
            if ($bridgeFailStreak -ge $bridgeFailuresRequired -and ($now - $lastBridgeRestart).TotalSeconds -ge $bridgeBaseCooldownSec) {
                Restart-Bridge
                $lastBridgeRestart = Get-Date
                $bridgeFailStreak = 0
                Start-Sleep -Seconds 10
            }
        }

        # --- Evolution API (segura a sessao Baileys - tratar com cautela) ---
        if (Test-Url "http://localhost:8080/" 5) {
            $evolutionFailStreak = 0

            # So considera "saudavel de verdade" e reseta o backoff apos um periodo sem falhas
            if (($now - $evolutionLastHealthyAt).TotalSeconds -ge $evolutionHealthyResetSec -and $evolutionCurrentCooldownSec -ne $evolutionBaseCooldownSec) {
                Write-Log "Evolution API estavel ha $($evolutionHealthyResetSec)s - resetando backoff de restart"
                $evolutionCurrentCooldownSec = $evolutionBaseCooldownSec
            }
        } else {
            if ($evolutionFailStreak -eq 0) {
                $evolutionLastHealthyAt = $now
            }
            $evolutionFailStreak++

            if ($evolutionFailStreak -ge $evolutionFailuresRequired) {
                if (($now - $lastEvolutionRestart).TotalSeconds -ge $evolutionCurrentCooldownSec) {
                    Restart-EvolutionApi -CooldownSec $evolutionCurrentCooldownSec
                    $lastEvolutionRestart = Get-Date
                    $evolutionFailStreak = 0
                    # backoff exponencial, limitado ao teto
                    $evolutionCurrentCooldownSec = [Math]::Min($evolutionCurrentCooldownSec * 2, $evolutionMaxCooldownSec)
                } else {
                    Write-Log "Evolution API fora do ar mas ainda em cooldown (proximo restart permitido em $([Math]::Round($evolutionCurrentCooldownSec - ($now - $lastEvolutionRestart).TotalSeconds))s) - aguardando"
                }
            }
        }
    } catch {
        Write-Log "Erro no ciclo de checagem: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $checkIntervalSec
}

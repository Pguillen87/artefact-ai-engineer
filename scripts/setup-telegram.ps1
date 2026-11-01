# Interactive local setup. The token is never printed or written to this repository.
# Windows User environment variables are stored in the user's registry in plaintext.
$ErrorActionPreference = 'Stop'
$tokenSecure = Read-Host 'Cole o token do bot (entrada oculta)' -AsSecureString
$pointer = [IntPtr]::Zero
$token = $null
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($tokenSecure)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'empty' }
    Write-Host 'No Telegram, envie /start ao bot em conversa privada antes de continuar.'
    [void](Read-Host 'Pressione Enter depois de enviar /start')
    try {
        $response = Invoke-RestMethod -Uri ("https://api.telegram.org/bot{0}/getUpdates?limit=100&timeout=0" -f $token) -Method Get -ErrorAction Stop
    } catch {
        throw 'Não foi possível consultar o Telegram. Verifique o token, webhook e outro processo de polling.'
    }
    if (-not $response.ok) { throw 'O Telegram não aceitou a consulta.' }
    $chatIds = @($response.result | ForEach-Object {
        if ($null -ne $_.message -and $_.message.chat.type -eq 'private') {
            [string]$_.message.chat.id
        }
    } | Sort-Object -Unique)
    if ($chatIds.Count -eq 0) { throw 'Nenhum chat privado encontrado. Envie /start e execute novamente.' }
    Write-Host 'IDs de chats privados encontrados:'
    $chatIds | ForEach-Object { Write-Host $_ }
    if ($chatIds.Count -eq 1) { $chosen = $chatIds[0] }
    else { $chosen = Read-Host 'Digite o ID do seu chat privado de teste' }
    if ($chosen -notmatch '^\d+$' -or $chosen -notin $chatIds) { throw 'ID inválido ou não encontrado.' }
    [Environment]::SetEnvironmentVariable('TELEGRAM_BOT_TOKEN', $token, 'User')
    [Environment]::SetEnvironmentVariable('TELEGRAM_ALLOWED_CHAT_ID', $chosen, 'User')
    Write-Host 'Variáveis de usuário configuradas. Avise o Codex; não envie o token no chat.'
} catch {
    $safeMessages = @('Não foi possível consultar o Telegram. Verifique o token, webhook e outro processo de polling.', 'O Telegram não aceitou a consulta.', 'Nenhum chat privado encontrado. Envie /start e execute novamente.', 'ID inválido ou não encontrado.')
    if ($_.Exception.Message -eq 'empty') { Write-Host 'Token vazio.' }
    elseif ($_.Exception.Message -in $safeMessages) { Write-Host $_.Exception.Message }
    else { Write-Host 'Falha na configuração local; nenhum detalhe sensível será exibido.' }
    exit 1
} finally {
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($null -ne $tokenSecure) { $tokenSecure.Dispose() }
    $token = $null
}

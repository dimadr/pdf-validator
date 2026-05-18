# PowerShell скрипт для установки корневого сертификата mkcert на Windows 11
# Запустите от имени Администратора: правый клик → "Запуск от имени администратора"

$ErrorActionPreference = "Stop"

$CertUrl = "http://192.168.0.107:8080/mkcert-ca.crt"
$TempCert = "$env:TEMP\mkcert-ca.crt"

Write-Host "Скачивание корневого сертификата..." -ForegroundColor Green

try {
    Invoke-WebRequest -Uri $CertUrl -OutFile $TempCert -UseBasicParsing
    Write-Host "Сертификат скачан" -ForegroundColor Green
} catch {
    Write-Host "Ошибка скачивания. Попробую curl..." -ForegroundColor Yellow
    curl.exe -o "$TempCert" "$CertUrl" 2>$null
    if (-not (Test-Path $TempCert)) {
        Write-Host "Ошибка: не удалось скачать сертификат" -ForegroundColor Red
        Write-Host "Скачайте вручную: $CertUrl" -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "Установка сертификата в доверенные корневые центры..." -ForegroundColor Green

try {
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($TempCert)
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store(
        [System.Security.Cryptography.X509Certificates.StoreName]::Root,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::LocalMachine
    )
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
    $store.Add($cert)
    $store.Close()
    
    Write-Host "Сертификат успешно установлен!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Теперь можно открывать https://192.168.0.107:4321 без ошибок SSL" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Важно: перезапустите браузер, если он был открыт" -ForegroundColor Yellow
} catch {
    Write-Host "Ошибка установки: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Установите вручную:" -ForegroundColor Yellow
    Write-Host "1. Откройте $TempCert" -ForegroundColor White
    Write-Host "2. 'Установить сертификат'" -ForegroundColor White
    Write-Host "3. 'Локальный компьютер' → 'Далее'" -ForegroundColor White
    Write-Host "4. 'Поместить все сертификаты в следующее хранилище'" -ForegroundColor White
    Write-Host "5. 'Обзор' → выберите 'Доверенные корневые центры сертификации'" -ForegroundColor White
    Write-Host "6. 'ОК' → 'Далее' → 'Готово'" -ForegroundColor White
}

# Очистка
if (Test-Path $TempCert) {
    Remove-Item $TempCert -Force
}

Write-Host ""
Write-Host "Нажмите любую клавишу для выхода..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

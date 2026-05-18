#!/bin/bash
# Скрипт установки корневого сертификата mkcert на клиентский компьютер
# Запустите на машине, с которой заходите на https://192.168.0.107:4321

set -e

CERT_URL="http://192.168.0.107:8080/mkcert-ca.crt"
TEMP_CERT="/tmp/mkcert-ca.crt"

echo "Скачивание корневого сертификата..."
curl -o "$TEMP_CERT" "$CERT_URL" 2>/dev/null || wget -O "$TEMP_CERT" "$CERT_URL" 2>/dev/null

if [ ! -f "$TEMP_CERT" ]; then
    echo "Ошибка: не удалось скачать сертификат"
    echo "Скачайте вручную: $CERT_URL"
    exit 1
fi

# Определение ОС
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if command -v update-ca-certificates &> /dev/null; then
        # Debian/Ubuntu
        sudo cp "$TEMP_CERT" /usr/local/share/ca-certificates/mkcert-ca.crt
        sudo update-ca-certificates
    elif command -v update-ca-trust &> /dev/null; then
        # RHEL/CentOS/Fedora
        sudo cp "$TEMP_CERT" /etc/pki/ca-trust/source/anchors/mkcert-ca.crt
        sudo update-ca-trust extract
    fi
    
    # Firefox
    if command -v certutil &> /dev/null; then
        for certDB in $(find ~/ -name "cert9.db" 2>/dev/null); do
            certdir=$(dirname "$certDB")
            certutil -A -n "mkcert-local-ca" -t "C,," -i "$TEMP_CERT" -d "sql:$certdir" 2>/dev/null || true
        done
    fi
    
    echo "Сертификат установлен в систему"
    
elif [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$TEMP_CERT"
    echo "Сертификат установлен в систему"
    
elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
    # Windows (Git Bash, Cygwin, MSYS)
    echo "Для Windows 11 используйте PowerShell скрипт:"
    echo "http://192.168.0.107:8080/install-ca-windows.ps1"
    echo ""
    echo "Или установите вручную:"
    echo "1. Дважды кликните по $TEMP_CERT"
    echo "2. Выберите 'Установить сертификат'"
    echo "3. 'Локальный компьютер' → 'Поместить все сертификаты в следующее хранилище'"
    echo "4. Выберите 'Доверенные корневые центры сертификации'"
else
    echo "Неизвестная ОС. Установите сертификат вручную: $TEMP_CERT"
fi

rm -f "$TEMP_CERT"
echo ""
echo "Теперь можно открывать https://192.168.0.107:4321 без ошибок SSL"

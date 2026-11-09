from email.header import decode_header

def decode_email_header(header: str) -> str:
    """Декодировать email заголовок из MIME формата"""
    if not header:
        return ""
    
    decoded_parts = decode_header(header)
    result = ""
    
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            if encoding:
                result += part.decode(encoding, errors='ignore')
            else:
                result += part.decode('utf-8', errors='ignore')
        else:
            result += part
    
    return result.strip()

import os
import httpx
import json
from typing import Dict, Any, Optional
from datetime import datetime
from celery_app import AI_API_KEY, AI_MODEL, AI_BASE_URL
from utils import logger

class UniversalAIClient:
    """Универсальный OpenAI-совместимый клиент для любого AI провайдера"""
    
    def __init__(self):
        self.api_key = AI_API_KEY
        self.model = AI_MODEL
        self.base_url = AI_BASE_URL.rstrip('/') if AI_BASE_URL else "https://routerai.ru/api/v1"
        
        if not self.api_key:
            logger.warning("AI_API_KEY not configured, skipping AI validation")
    
    async def validate_document(self, document_text: str, tables_data: list,
                                validation_result: Optional[Dict] = None) -> Dict[str, Any]:
        """Универсальная валидация документа через AI"""
        
        if not self.api_key:
            return self._mock_response()
        
        try:
            prompt = self._build_prompt(document_text, tables_data, validation_result)
            response = await self._make_request(prompt)
            return self._parse_response(response)
            
        except Exception as e:
            logger.error(f"AI validation failed: {e}")
            return self._mock_response()
    
    def _build_prompt(self, document_text: str, tables_data: list,
                      validation_result: Optional[Dict] = None) -> str:
        """Создание промпта для валидации"""
        
        # Ограничиваем длину текста
        max_chars = 2000
        truncated_text = document_text[:max_chars]
        if len(document_text) > max_chars:
            truncated_text += "...[truncated]"
        
        # Ограничиваем количество таблиц
        max_tables = 3
        truncated_tables = tables_data[:max_tables]
        
        # Текущий месяц на русском
        months_ru = ['', 'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
                     'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
        now = datetime.now()
        current_month_str = f"{months_ru[now.month]} {now.year}"
        
        # Детерминированный контекст (если доступен)
        deterministic_context = ""
        if validation_result:
            pages = validation_result.get('pages_count', 'N/A')
            tables_count = len(tables_data)
            det_dates = validation_result.get('deterministic_dates_ok', False)
            det_tables = validation_result.get('deterministic_tables_ok', False)
            deterministic_context = (
                f"\nКОНТЕКСТ ДЕТЕРМИНИРОВАННОЙ ПРОВЕРКИ:\n"
                f"- Страниц в документе: {pages}\n"
                f"- Найдено таблиц: {tables_count}\n"
                f"- Даты валидны: {'да' if det_dates else 'нет'}\n"
                f"- Таблицы без пропусков: {'да' if det_tables else 'нет'}\n"
            )
        
        prompt = f"""Проанализируй документ и ответь строго в формате JSON.

Текст документа:
---
{truncated_text}
---

Таблицы (максимум {max_tables}):
{json.dumps(truncated_tables, ensure_ascii=False, indent=2)}
{deterministic_context}
Задания:
1. dates_match_current_month — содержит ли документ даты текущего месяца ({current_month_str})
2. all_table_cells_are_numbers — все ли ячейки таблиц содержат только числовые значения
3. document_is_valid — является ли документ корректным в целом
4. confidence_score — уверенность в валидации (0.0–1.0)
5. notes — краткий комментарий (максимум 200 символов)
6. detected_document_type — тип документа (счёт, акт, отчёт, и т.д.)

Ответь строго в этом JSON формате (без дополнительного текста):
{{
    "dates_match_current_month": true/false,
    "all_table_cells_are_numbers": true/false,
    "document_is_valid": true/false,
    "confidence_score": 0.0-1.0,
    "notes": "Краткий комментарий",
    "detected_document_type": "тип документа"
}}"""
        
        return prompt
    
    async def _make_request(self, prompt: str) -> Dict[str, Any]:
        """Отправка запроса к AI API"""
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://email-processor.local",
            "X-Title": "Email Processor PDF Validator"
        }
        
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты - эксперт по анализу документов. Отвечай только в указанном JSON формате."
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            "temperature": 0.1,
            "max_tokens": 500
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            return response.json()
    
    def _parse_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Парсинг OpenAI-совместимого ответа"""
        
        try:
            content = response["choices"][0]["message"]["content"]
            
            # Пытаемся распарсить JSON
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return json.loads(content)
                
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to parse AI response: {e}, raw: {response}")
            return self._mock_response()
    
    def _mock_response(self) -> Dict[str, Any]:
        """Заглушка когда AI недоступен"""
        return {
            "dates_match_current_month": True,
            "all_table_cells_are_numbers": True,
            "document_is_valid": True,
            "confidence_score": 0.0,
            "notes": "AI validation skipped - using deterministic validation only",
            "detected_document_type": "unknown"
        }

# Создаем глобальный экземпляр
ai_client = UniversalAIClient()
"""
kontur.py — интеграция с API Контур.Закупок (https://api-zakupki.kontur.ru/)

Подключается к существующему app.py как Blueprint:

    from kontur import kontur_bp
    app.register_blueprint(kontur_bp)

Переменная окружения (задать в Render → Environment):
    KONTUR_API_KEY = <ваш ключ>

Эндпоинты:
    GET  /kontur/limits            — проверка ключа и суточных лимитов (дешёвый pre-flight)
    POST /kontur/search            — поиск закупок (тело = параметры ExternalApiSearchArgs)
    GET  /kontur/purchase/<id>     — детали закупки (JSON со ссылками Docs[].Url; БЕЗ скачивания)

Архитектура раздельная: этот модуль только получает данные из Контура и отдаёт чистый JSON.
Скачивание/OCR документов делает существующий парсер по ссылкам из Docs[].Url.
"""

import os
import threading

import requests
from flask import Blueprint, jsonify, request

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

BASE_URL = "https://api-zakupki.kontur.ru/external/v1"
API_KEY = os.environ.get("KONTUR_API_KEY", "")

# Таймаут на исходящий запрос к Контуру (сек). search/purchase отвечают быстро,
# но запас нужен, чтобы не висеть бесконечно на сетевых проблемах.
REQUEST_TIMEOUT = 30

# Контур: не более 5 одновременных обращений к серверу (тех. ограничение API).
# Семафор каплит общее число параллельных исходящих вызовов в рамках процесса.
_kontur_semaphore = threading.Semaphore(5)

# Маркер «закрашенных» данных (BlurredApiSchema). На тестовом ключе многие поля
# приходят как "#####". В выводе пользователя это могло отображаться как "░".
_BLUR_CHARS = set("#░")

kontur_bp = Blueprint("kontur", __name__, url_prefix="/kontur")


# --------------------------------------------------------------------------- #
# Хелперы очистки ответа
# --------------------------------------------------------------------------- #

def _is_blurred(value):
    """True, если строка целиком состоит из маркеров блюра (#### / ░░░)."""
    if not isinstance(value, str) or value == "":
        return False
    return all(ch in _BLUR_CHARS for ch in value)


def _is_empty_date(value):
    """True для 'пустой' даты Контура 0001-01-01... (не задано)."""
    return isinstance(value, str) and value.startswith("0001-01-01")


def _clean(obj):
    """
    Рекурсивно нормализует ответ Контура:
      - заблюренные значения ("#####"/"░░░")        -> None
      - пустые даты "0001-01-01T00:00:00"           -> None
    Остальное оставляет как есть. Структуру (dict/list) сохраняет.
    """
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, str):
        if _is_blurred(obj) or _is_empty_date(obj):
            return None
        return obj
    return obj


# --------------------------------------------------------------------------- #
# Низкоуровневый вызов Контура
# --------------------------------------------------------------------------- #

def _headers():
    return {
        "X-Kontur-Apikey": API_KEY,
        "Content-Type": "application/json",
    }


def _call_kontur(method, path, *, json_body=None):
    """
    Выполняет запрос к Контуру с учётом лимита параллелизма и обработкой ошибок.

    Возвращает кортеж (payload, status_code), где payload — dict, готовый к jsonify:
      - при успехе: очищенные данные Контура (как они есть, dict или {"items": [...]})
      - при ошибке: {"error": "...", "detail": "..."} с подходящим status_code
    """
    if not API_KEY:
        return ({"error": "config", "detail": "KONTUR_API_KEY не задан в окружении"}, 500)

    url = f"{BASE_URL}{path}"

    try:
        with _kontur_semaphore:
            resp = requests.request(
                method,
                url,
                headers=_headers(),
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
    except requests.Timeout:
        return ({"error": "timeout", "detail": f"Контур не ответил за {REQUEST_TIMEOUT}с"}, 504)
    except requests.RequestException as exc:
        return ({"error": "network", "detail": str(exc)}, 502)

    # --- Обработка статусов Контура ------------------------------------------------
    if resp.status_code == 429:
        # Превышен суточный лимит метода ИЛИ слишком много одновременных запросов.
        # НЕ ретраим вслепую: суточный счётчик восстановится в 00:00.
        return ({
            "error": "rate_limit",
            "detail": "Контур: превышен лимит (429). Суточный сброс в 00:00 UTC, "
                      "либо превышено 5 одновременных запросов.",
        }, 429)

    if resp.status_code in (401, 403):
        return ({
            "error": "auth",
            "detail": f"Контур: {resp.status_code}. Ключ истёк/отозван или нет доступа "
                      f"к методу по тарифу.",
        }, resp.status_code)

    if resp.status_code != 200:
        return ({
            "error": "kontur",
            "detail": f"Контур вернул {resp.status_code}",
            "body": resp.text[:500],
        }, resp.status_code)

    try:
        data = resp.json()
    except ValueError:
        return ({"error": "bad_json", "detail": "Контур вернул не-JSON", "body": resp.text[:500]}, 502)

    return (_clean(data), 200)


# --------------------------------------------------------------------------- #
# Эндпоинты
# --------------------------------------------------------------------------- #

@kontur_bp.route("/limits", methods=["GET"])
def kontur_limits():
    """
    Pre-flight проверка: жив ли ключ и сколько запросов осталось по группам
    (search / purchase / results). Дешёвый вызов, тратить можно свободно.
    """
    data, status = _call_kontur("GET", "/limitGroups")
    return jsonify(data), status


@kontur_bp.route("/search", methods=["POST"])
def kontur_search():
    """
    Поиск закупок. Тело запроса = параметры ExternalApiSearchArgs (как есть).
    Обязательны DateTimeFrom и DateTimeTo.

    Пример тела:
    {
      "DateTimeFrom": "2026-06-13T00:00:00Z",
      "DateTimeTo":   "2026-06-16T23:59:59Z",
      "CategoryIds":  [234, 222, 135, 76, 386],
      "Text":         ["чиллер", "сплит-система", "фанкойл", "драйкулер"],
      "Exclude":      ["обслуживание", "ремонт", "капитальный"],
      "Laws":         [3, 8, 9],
      "PurchaseStatuses": [1],
      "PageNumber":   0
    }

    Возвращает {PageNumber, TotalCount, Items:[...]} с очищенными значениями.
    """
    body = request.get_json(silent=True) or {}

    # Контур требует диапазон дат — проверяем заранее, чтобы не жечь лимит впустую.
    missing = [f for f in ("DateTimeFrom", "DateTimeTo") if not body.get(f)]
    if missing:
        return jsonify({
            "error": "validation",
            "detail": f"Обязательные поля не заданы: {', '.join(missing)}",
        }), 400

    data, status = _call_kontur("POST", "/search", json_body=body)
    return jsonify(data), status


@kontur_bp.route("/purchase/<purchase_id>", methods=["GET"])
def kontur_purchase(purchase_id):
    """
    Детали одной закупки по Id.
    Отдаёт JSON: характеристики (Lots[].Customers[].Objects[].Characteristics),
    ссылки на документы (Docs[].Url), требования (TenderRequirements), сроки и т.д.

    Скачивание файлов НЕ выполняется — это задача существующего парсера,
    которому передаются ссылки из Docs[].Url.

    Тратит 1 запрос из суточного лимита группы 'purchase'.
    """
    purchase_id = (purchase_id or "").strip()
    if not purchase_id:
        return jsonify({"error": "validation", "detail": "Пустой id закупки"}), 400

    data, status = _call_kontur("GET", f"/purchases/{purchase_id}")
    return jsonify(data), status

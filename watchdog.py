import os
import time
import logging
import requests
import json

# --- Config from env ---
SILLI_URL        = os.environ.get("SILLI_URL", "https://ai-office-shared-production.up.railway.app").rstrip("/")
RAILWAY_TOKEN    = os.environ["RAILWAY_TOKEN"]
SILLI_SERVICE_ID = os.environ["SILLI_SERVICE_ID"]
SILLI_ENV_ID     = os.environ["SILLI_ENV_ID"]
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID          = os.environ["TELEGRAM_CHAT_ID"]

# Трейдер (tilly-trader) — второй сторожимый сервис. SERVICE_ID/ENV_ID опциональны:
# без них контур работает в режиме «только алерт» (без авто-редеплоя).
TRADER_URL        = os.environ.get("TRADER_URL", "https://tilly-trader-production.up.railway.app").rstrip("/")
TRADER_SERVICE_ID = os.environ.get("TRADER_SERVICE_ID", "")
TRADER_ENV_ID     = os.environ.get("TRADER_ENV_ID", "")

CHECK_INTERVAL        = 120   # секунд между проверками
FAIL_THRESHOLD        = 2     # фейлов подряд до редеплоя (Силли)
TRADER_FAIL_THRESHOLD = 3     # деградаций подряд до реакции (трейдер); ~6 мин при 120с
REDEPLOY_COOLDOWN     = 300   # секунд паузы после редеплоя

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def check_health() -> bool:
    """GET /health → 200 OK = жива. Любая ошибка/таймаут = упала."""
    try:
        r = requests.get(f"{SILLI_URL}/health", timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Health check exception: {e}")
        return False


def redeploy_service(service_id: str, env_id: str) -> bool:
    mutation = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    try:
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            json={"query": mutation, "variables": {
                "serviceId": service_id,
                "environmentId": env_id
            }},
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            timeout=30
        )
        data = resp.json()
        return not data.get("errors")
    except Exception as e:
        log.error(f"Redeploy request failed: {e}")
        return False


def redeploy_silli() -> bool:
    return redeploy_service(SILLI_SERVICE_ID, SILLI_ENV_ID)


def check_trader() -> tuple:
    """GET трейдер /health. Возвращает (ok, reason).

    Трейдер теперь отдаёт 503 при деградации (stale-скан / выключенный скринер) и
    кладёт причину в тело — раньше /health всегда был 200 и стоп сканера был невидим.
    """
    try:
        r = requests.get(f"{TRADER_URL}/health", timeout=10)
        if r.status_code == 200:
            return True, None
        reason = str(r.status_code)
        try:
            b = r.json()
            reason = b.get("reason") or b.get("status") or reason
        except Exception:
            pass
        return False, reason
    except Exception as e:
        return False, f"timeout:{type(e).__name__}"



PLATFORM_OUTAGE_SILENCE = 1800  # 30 мин тишины после обнаружения outage
_platform_outage_alerted = False  # уже отправили алерт об outage
_platform_outage_until   = 0      # молчим до этого timestamp


def check_railway_platform_status() -> str:
    """
    Проверяет https://status.railway.app/api/v2/status.json
    Возвращает: "ok" | "incident" | "major_outage" | "unknown"
    Timeout 8 сек — не блокируем основной цикл надолго.
    """
    try:
        r = requests.get("https://status.railway.app/api/v2/status.json", timeout=8)
        if r.status_code != 200:
            return "unknown"
        data = r.json()
        indicator = data.get("status", {}).get("indicator", "none").lower()
        # Cloudflare statuspage: none / minor / major / critical
        if indicator in ("major", "critical"):
            return "major_outage"
        if indicator in ("minor",):
            return "incident"
        return "ok"
    except Exception as e:
        log.warning(f"Platform status check failed: {e}")
        return "unknown"


def tg(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def notify_team(message: str) -> bool:
    """Уведомляет команду разработки (Девви) что лидер упал и нужна починка."""
    try:
        devvy_url = os.environ.get("DEVVY_URL", "https://devvy-bot-production-9a4f.up.railway.app")
        resp = requests.post(
            f"{devvy_url}/task",
            json={"message": message, "user_id": 391077101},
            timeout=15
        )
        log.info(f"Team notified: {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Team notify failed: {e}")
        return False


def main():
    log.info("Railway Watchdog запущен (второй слой защиты после Cloudflare).")

    fail_count = 0
    in_redeploy = False
    platform_outage_alerted = False
    platform_outage_until   = 0
    api_fail_count = 0  # счётчик Railway API failures — не спамим

    trader_fail = 0
    trader_in_redeploy = False  # «реакция уже была» — молчим до восстановления

    while True:
        healthy = check_health()

        if healthy:
            if in_redeploy:
                log.info("Силли восстановилась!")
                tg("✅ <b>Силли восстановилась</b> (Railway Watchdog)")
                in_redeploy = False
            fail_count = 0
            api_fail_count = 0  # сбрасываем при восстановлении
            log.info("OK")
        else:
            fail_count += 1
            log.warning(f"Силли не отвечает. Fail {fail_count}/{FAIL_THRESHOLD}")

            if fail_count >= FAIL_THRESHOLD and not in_redeploy:
                # ── Platform outage detection ─────────────────────────────
                # Перед алертом проверяем status.railway.app
                # Major Outage → один алерт + тишина 30 мин (не спамим)
                now = time.time()
                if now < platform_outage_until:
                    # Ещё в периоде молчания после outage — пропускаем
                    log.info(f"Platform outage silence active, skipping alert")
                    time.sleep(CHECK_INTERVAL)
                    continue

                platform_status = check_railway_platform_status()
                log.info(f"Platform status: {platform_status}")

                if platform_status == "major_outage":
                    if not platform_outage_alerted:
                        tg("🌐 <b>Railway Platform Outage</b> — глобальный сбой на стороне Railway. Силли не отвечает из-за этого. Жду восстановления, алертов не будет.")
                        platform_outage_alerted = True
                    platform_outage_until = now + PLATFORM_OUTAGE_SILENCE
                    fail_count = 0
                    time.sleep(PLATFORM_OUTAGE_SILENCE)
                    platform_outage_alerted = False  # сбрасываем чтобы алертнуть если повторится
                    continue

                # Платформа ок (или unknown) — обычный алерт и редеплой
                platform_outage_alerted = False
                log.error("Порог достигнут. Запускаю редеплой...")
                tg(f"⚠️ <b>Railway Watchdog:</b> Силли не отвечает {fail_count} раза подряд. Редеплой...")

                if redeploy_silli():
                    tg("🚀 Редеплой запущен.")
                    in_redeploy = True
                    fail_count = 0
                    time.sleep(REDEPLOY_COOLDOWN)
                    # Проверяем восстановилась ли Силли после редеплоя
                    recovered = check_health()
                    if recovered:
                        log.info("Силли восстановилась после редеплоя")
                        tg("✅ <b>Силли восстановилась</b> после редеплоя.")
                        in_redeploy = False
                    else:
                        # Редеплой не помог — код сломан, нужна команда
                        log.error("Силли не восстановилась после редеплоя — код сломан")
                        tg(
                            "🔴 <b>Силли не восстановилась после редеплоя.</b>\n"
                            "Вероятно сломан код. Уведомляю команду..."
                        )
                        team_msg = (
                            "СРОЧНО: Силли (ai-office-shared) упала и не восстановилась после редеплоя. "
                            "Код сломан. Нужно: 1) прочитать логи Railway сервиса ai-office-shared, "
                            "2) найти причину краша в agents/coder.py, "
                            "3) исправить и задеплоить. "
                            "Railway service: 95999005-f1a9-4ce9-9cee-7e803394e14e, "
                            "project: dev-dept (30a933d1-689f-4709-a12c-a36a49aa1820)."
                        )
                        notify_team(team_msg)
                    continue
                else:
                    api_fail_count += 1
                    if api_fail_count <= 1:
                        tg("🔴 <b>Railway API недоступен.</b> Нужно ручное вмешательство.")
                    # После первого алерта — молчим 6 часов, не повторяем
                    in_redeploy = True
                    fail_count = 0
                    time.sleep(21600)    # 6 часов тишины вместо 30 мин цикла
                    in_redeploy = False  # снова мониторим
                    continue

        # ── Трейдер (tilly-trader): отдельный лёгкий контур ───────────────
        # Цель — поймать «молчание» сканера, которое раньше было невидимым:
        # screener_disabled (нет TRADING_CHANNEL_ID) или scan_stale (скан завис).
        t_ok, t_reason = check_trader()
        if t_ok:
            if trader_in_redeploy:
                tg("✅ <b>Трейдер восстановился</b> (Railway Watchdog)")
                trader_in_redeploy = False
            trader_fail = 0
        else:
            trader_fail += 1
            log.warning(f"Трейдер degraded ({t_reason}). Fail {trader_fail}/{TRADER_FAIL_THRESHOLD}")
            if trader_fail >= TRADER_FAIL_THRESHOLD and not trader_in_redeploy:
                if t_reason == "screener_disabled":
                    # Конфиг (нет TRADING_CHANNEL_ID) — редеплой НЕ поможет, нужна команда.
                    tg("🛑 <b>Трейдер: скринер ВЫКЛЮЧЕН</b> (screener_disabled).\n"
                       "Сигналы не идут. Нужен TRADING_CHANNEL_ID в env — редеплой не починит.")
                    notify_team(
                        "Трейдер tilly-trader: /health=screener_disabled — не задан "
                        "TRADING_CHANNEL_ID, сигналы не идут вообще. Проверь env на Railway."
                    )
                elif TRADER_SERVICE_ID and TRADER_ENV_ID:
                    tg(f"⚠️ <b>Трейдер деградировал</b> ({t_reason}) ×{trader_fail}. Редеплой...")
                    if redeploy_service(TRADER_SERVICE_ID, TRADER_ENV_ID):
                        tg("🚀 Редеплой трейдера запущен.")
                    else:
                        tg("🔴 Редеплой трейдера не удался (Railway API).")
                else:
                    # Авто-редеплой выключен (нет TRADER_SERVICE_ID) — режим «только алерт».
                    tg(f"⚠️ <b>Трейдер деградировал</b> ({t_reason}) ×{trader_fail}.\n"
                       "Авто-редеплой выключен (нет TRADER_SERVICE_ID) — нужен ручной разбор.")
                    notify_team(f"Трейдер tilly-trader /health degraded: {t_reason}. Проверь логи и /health.")
                trader_in_redeploy = True  # молчим до восстановления
                trader_fail = 0

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()

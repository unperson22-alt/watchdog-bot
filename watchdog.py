import os
import time
import logging
import requests

# --- Config from env ---
SILLI_URL        = os.environ.get("SILLI_URL", "https://ai-office-shared-production.up.railway.app").rstrip("/")
RAILWAY_TOKEN    = os.environ["RAILWAY_TOKEN"]
SILLI_SERVICE_ID = os.environ["SILLI_SERVICE_ID"]
SILLI_ENV_ID     = os.environ["SILLI_ENV_ID"]
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID          = os.environ["TELEGRAM_CHAT_ID"]

# Трейдер (tilly-trader) — второй сторожимый сервис. Railway service/env заданы
# дефолтами (как в офисном coder.py: SERVICES/SERVICE_ENV) — авто-редеплой работает
# из коробки; env-переменные при необходимости их переопределяют.
TRADER_URL        = os.environ.get("TRADER_URL", "https://tilly-trader-production.up.railway.app").rstrip("/")
TRADER_SERVICE_ID = os.environ.get("TRADER_SERVICE_ID", "1c08bbcc-32bb-4e91-9bc9-d196c937c1c4")
TRADER_ENV_ID     = os.environ.get("TRADER_ENV_ID", "7ff2ff7a-b6d7-4c06-95c9-9958f0d3af7b")

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


def railway(query: str, variables: dict = None) -> dict:
    """Один вход в Railway GraphQL. Возвращает {} при любой сетевой беде."""
    try:
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        return resp.json() or {}
    except Exception as e:
        log.error(f"Railway request failed: {e}")
        return {}


# ── Последний ЗДОРОВЫЙ деплой ───────────────────────────────────────────────
# ЗАЧЕМ: сторож умел ровно один приём — serviceInstanceRedeploy, а он
# пересобирает ТОТ ЖЕ коммит. Если Силли легла из-за сломанного кода (инцидент
# 01.07: файл на 5766 строк заменился заглушкой на 8 и уехал в прод), редеплой
# честно поднимает ту же заглушку и снова падает. Отсюда и тупик «редеплой не
# помог» — сторож не мог вернуть офис в рабочее состояние в принципе.
#
# ПОЧЕМУ «здоровый», а не «SUCCESS»: статус деплоя говорит, что СБОРКА удалась.
# Процесс при этом может падать на импорте. Единственное честное доказательство
# работоспособности — что /health отвечал ПОСЛЕ этого деплоя, и знает об этом
# только сторож, потому что он единственный, кто это проверяет.
LAST_GOOD_FILE   = os.environ.get("LAST_GOOD_FILE", "/tmp/watchdog_last_good")
GOOD_REFRESH_EVERY = 10          # циклов между обновлениями отметки (~20 мин)
DEPLOY_GRACE_SEC   = 240         # сколько даём новому деплою подняться


def current_deployment(service_id: str) -> dict:
    """Текущий (самый свежий) деплой сервиса: {id, status} или {}."""
    data = railway(
        "query($sid:String!){deployments(first:1,input:{serviceId:$sid})"
        "{edges{node{id status}}}}",
        {"sid": service_id})
    edges = (((data.get("data") or {}).get("deployments") or {}).get("edges") or [])
    return edges[0]["node"] if edges else {}


def remember_good(deployment_id: str) -> None:
    """Запомнить деплой, при котором /health реально отвечал."""
    if not deployment_id:
        return
    try:
        with open(LAST_GOOD_FILE, "w") as f:
            f.write(deployment_id)
    except Exception as e:
        log.warning(f"Не смог записать отметку последнего здорового деплоя: {e}")


def load_last_good() -> str:
    """Отметка с диска. Пусто — значит сторож ещё не видел Силли живой."""
    try:
        with open(LAST_GOOD_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def rollback_to(deployment_id: str) -> bool:
    """
    Откатить сервис на конкретный деплой.

    Пробуем deploymentRollback, при отказе — deploymentRedeploy. Обе мутации
    есть в схеме Railway (проверено интроспекцией), но сигнатуры со временем
    менялись; ошибка валидации GraphQL приходит ДО любого эффекта, поэтому
    вторая попытка безопасна. Зовётся только когда Силли уже лежит и обычный
    редеплой не помог — не сделать ничего здесь хуже, чем попробовать оба.
    """
    for mutation, field in (
        ("mutation($id:String!){deploymentRollback(id:$id)}", "deploymentRollback"),
        ("mutation($id:String!){deploymentRedeploy(id:$id)}", "deploymentRedeploy"),
    ):
        data = railway(mutation, {"id": deployment_id})
        if data and not data.get("errors"):
            log.info(f"Откат выполнен через {field}")
            return True
        log.warning(f"{field} не сработал: {str(data.get('errors'))[:200]}")
    return False


def rollback_silli_to_last_good() -> str:
    """
    Вернуть Силли на последний деплой, при котором она была ЖИВА.

    Возвращает человеко-читаемый итог для алерта — сторож обязан говорить, что
    именно он сделал, иначе автоматическое действие неотличимо от случайности.
    """
    good = load_last_good()
    if not good:
        return "откатывать некуда: сторож ещё не видел Силли здоровой"
    cur = current_deployment(SILLI_SERVICE_ID)
    if cur.get("id") == good:
        return f"откат бессмысленен: сейчас и так развёрнут {good[:8]} (он и был здоровым)"
    if not rollback_to(good):
        return f"откат на {good[:8]} НЕ прошёл — Railway отклонил обе мутации"
    time.sleep(DEPLOY_GRACE_SEC)
    if check_health():
        return f"откат на {good[:8]} помог — Силли отвечает"
    return f"откат на {good[:8]} прошёл, но /health молчит — проблема не в коде Силли"


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
    """
    Позвать dev-отдел чинить лидера.

    Раньше это был POST в никуда: код ответа писался в лог, `source` не
    ставился, а если Девви тоже лежал — сообщение просто исчезало. Тот же
    класс, что и молчаливый отказ вайтлиста: сбой без следа не отлаживается,
    а здесь он ещё и означал, что чинить Силли не придёт вообще никто.

    Теперь: проверяем ответ, и о неудаче докладываем в Telegram — там сидит
    человек, и он последняя инстанция, когда лежат и лидер, и команда.

    Доска задач сюда сознательно НЕ подключена: у сторожа нет REDIS_URL, а
    выдавать ему доступ к общей памяти ради одной записи — расширять права
    компонента, вся ценность которого в том, что он маленький и переживает
    смерть остальных.
    """
    devvy_url = os.environ.get("DEVVY_URL", "https://devvy-bot-production-9a4f.up.railway.app")
    try:
        resp = requests.post(
            f"{devvy_url}/task",
            json={"message": message, "user_id": 391077101, "source": "WATCHDOG",
                  "sender": "ВАЧДОГ"},
            timeout=15,
        )
        ok = resp.status_code in (200, 202)
        log.info(f"Team notified: {resp.status_code}")
        if not ok:
            tg(f"🔴 <b>Dev-отдел не принял задачу</b> (Девви ответил {resp.status_code}).\n"
               f"Силли лежит, команда недоступна — нужен ты, шеф.")
        return ok
    except Exception as e:
        log.warning(f"Team notify failed: {e}")
        tg(f"🔴 <b>Dev-отдел недоступен</b> ({type(e).__name__}).\n"
           f"Силли лежит и позвать команду не получилось — нужен ты, шеф.")
        return False


def preflight_service(service_id: str, label: str) -> bool:
    """
    Проверить, что сторожимый сервис вообще существует на Railway.

    16.08.2026: выяснилось, что SILLI_SERVICE_ID указывает на сервис с
    deletedAt = 2026-05-30 — serviceInstances пуст, деплоев ноль. То есть с
    конца мая каждый редеплой уходил в удалённую запись, current_deployment
    возвращал {}, отметка последнего здорового деплоя не ставилась ни разу, а
    значит и откат был невозможен. Сторож при этом рапортовал штатно: пустой
    ответ обрабатывался как «нечего запоминать» и в лог не попадал.

    Мёртвый идентификатор снаружи неотличим от рабочего, пока не случится
    авария — а в аварию как раз и выясняется, что сторожа нет. Поэтому
    спрашиваем один раз на старте и, если сервиса нет, говорим об этом
    человеку: сам сторож эту переменную починить не может.
    """
    if not service_id:
        tg(f"🔴 <b>Сторож без цели</b>: не задан id сервиса «{label}». "
           f"Автоподъём не работает.")
        return False

    # Три исхода, и путать их нельзя. «Railway не ответил» — не улика против
    # переменной: обвинить её из-за сетевой моргалки значит послать человека
    # чинить исправное. Поэтому недоступность API сначала пережидаем, а если
    # так и не ответил — говорим именно это, а не «сервис не найден».
    data = {}
    for attempt in range(3):
        data = railway("query($sid:String!){service(id:$sid){name deletedAt}}",
                       {"sid": service_id})
        if data.get("data") is not None:
            break
        if attempt < 2:
            time.sleep(5 * (attempt + 1))
    if data.get("data") is None:
        log.error(f"[preflight] {label}: Railway API не ответил, проверить не смог")
        tg(f"⚠️ <b>Сторож не смог проверить цель</b>: Railway API не ответил "
           f"на запрос о сервисе «{label}». Слежу дальше, но подтвердить, что "
           f"редеплой сработает, не могу.")
        return False

    svc = data["data"].get("service")
    if not svc:
        log.error(f"[preflight] {label}: сервис {service_id[:8]}… не найден")
        tg(f"🔴 <b>Сторож целится в никуда</b>: сервис «{label}» "
           f"({service_id[:8]}…) не найден на Railway. Автоподъём не работает "
           f"— проверь переменную, шеф.")
        return False
    if svc.get("deletedAt"):
        log.error(f"[preflight] {label}: сервис удалён {svc['deletedAt']}")
        tg(f"🔴 <b>Сторож целится в удалённый сервис</b>: «{label}» "
           f"({service_id[:8]}…) удалён {str(svc['deletedAt'])[:10]}.\n"
           f"Редеплой и откат не сработают. Возьми настоящий id в Railway UI "
           f"и положи в переменную, шеф.")
        return False
    log.info(f"[preflight] {label}: сервис «{svc.get('name', '?')}» на месте")
    return True


def main():
    log.info("Railway Watchdog запущен (второй слой защиты после Cloudflare).")

    # Проверяем цель ДО первого цикла: сторож, который не может дотянуться до
    # сторожимого, обязан сказать об этом сразу, а не в момент аварии.
    preflight_service(SILLI_SERVICE_ID, "Силли")
    preflight_service(TRADER_SERVICE_ID, "Трейдер")

    fail_count = 0
    in_redeploy = False
    platform_outage_alerted = False
    platform_outage_until   = 0
    api_fail_count = 0  # счётчик Railway API failures — не спамим

    trader_fail = 0
    trader_in_redeploy = False  # «реакция уже была» — молчим до восстановления

    good_tick = 0        # счётчик здоровых циклов до обновления отметки
    seen_deploy = ""     # последний ВИДЕННЫЙ деплой — чтобы заметить новый выкат

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

            # Отметка «этот деплой точно рабочий». Раз в GOOD_REFRESH_EVERY
            # циклов, а не каждый — иначе сторож молотит Railway API впустую.
            good_tick += 1
            if good_tick >= GOOD_REFRESH_EVERY or not seen_deploy:
                good_tick = 0
                cur = current_deployment(SILLI_SERVICE_ID)
                cur_id = cur.get("id", "")
                if cur_id:
                    if seen_deploy and cur_id != seen_deploy:
                        # Приехал новый выкат и Силли после него жива — это и
                        # есть успешная доставка. Отдельно сообщать не о чем.
                        log.info(f"Новый деплой {cur_id[:8]} здоров")
                    seen_deploy = cur_id
                    remember_good(cur_id)
                else:
                    # Силли отвечает на /health, но деплоев у сервиса нет —
                    # значит опрашиваем не тот сервис. Ровно так выглядел
                    # мёртвый SILLI_SERVICE_ID с 30.05: ветка молча ничего не
                    # делала, отметка здорового деплоя не ставилась ни разу,
                    # и откатывать в аварию было не на что.
                    log.error("Сервис жив по /health, но деплоев у него нет "
                              "— id почти наверняка чужой или удалён")
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
                    log.info("Platform outage silence active, skipping alert")
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
                        # Курьер обязан уметь отыграть назад. Раньше здесь
                        # был тупик: редеплой пересобрал тот же сломанный
                        # коммит, значит и второй раз упадёт. Сначала возвращаем
                        # офис в рабочее состояние, и только потом зовём людей —
                        # чинить сломанное приятнее, когда прод уже жив.
                        verdict = rollback_silli_to_last_good()
                        log.warning(f"Откат: {verdict}")
                        tg(f"↩️ <b>Откат Силли:</b> {verdict}")
                        if check_health():
                            in_redeploy = False
                            fail_count = 0
                            seen_deploy = current_deployment(SILLI_SERVICE_ID).get("id", "")

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

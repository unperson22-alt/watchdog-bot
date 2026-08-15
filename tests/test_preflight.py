"""
Проверка цели сторожа на старте — инцидент 2026-08-16.

Выяснилось, что SILLI_SERVICE_ID указывает на сервис, удалённый 30.05.2026:
deletedAt проставлен, serviceInstances пуст, деплоев ноль. Значит с конца мая
каждый редеплой уходил в удалённую запись, current_deployment возвращал {},
отметка последнего здорового деплоя не ставилась ни разу — и откатывать в
аварию было не на что. Сторож при этом рапортовал штатно: пустой ответ
обрабатывался как «нечего запоминать» и в лог не попадал.

Мёртвый id снаружи неотличим от рабочего, пока не случится авария — а в
аварию как раз и выясняется, что сторожа нет. Отсюда preflight_service.

Отдельный тест — на то, что недоступность Railway API НЕ выдаётся за
удалённый сервис. Обвинить исправную переменную из-за сетевой моргалки значит
послать человека чинить работающее; это тот же ложный вывод, только с другим
знаком.

watchdog.py не импортируется: модуль требует RAILWAY_TOKEN и остальные env,
а main() уходит в бесконечный цикл. Функцию достаём из AST и исполняем с
подменёнными tg/log/railway/time.

Запуск: cd watchdog-bot && python3 -m unittest discover -s tests -v
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "watchdog.py")

DEAD_ID = "efa6bd21-91d8-467f-8250-60f8a3853791"   # тот самый, из инцидента


class _Log:
    def __init__(self):
        self.lines = []

    def error(self, m):
        self.lines.append(m)

    def info(self, m):
        self.lines.append(m)

    def warning(self, m):
        self.lines.append(m)


class _NoSleep:
    def sleep(self, _s):
        """Ретраи проверяем по числу вызовов, а не по часам."""


def load_preflight(railway):
    """preflight_service из настоящего watchdog.py, с подменённым окружением."""
    with open(SRC, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "preflight_service"),
                None)
    if node is None:
        raise AssertionError("preflight_service не найдена в watchdog.py")
    sent, log = [], _Log()
    ns = {"tg": sent.append, "log": log, "railway": railway, "time": _NoSleep()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns["preflight_service"], sent, log


class TestPreflight(unittest.TestCase):
    def test_deleted_service_is_reported(self):
        # РОВНО инцидент: id живёт в переменной, сервиса за ним нет с мая.
        pf, sent, _ = load_preflight(
            lambda q, v: {"data": {"service": {
                "name": "cilly-bot-5f690e3f-…",
                "deletedAt": "2026-05-30T06:34:38.480Z"}}})
        self.assertFalse(pf(DEAD_ID, "Силли"))
        self.assertEqual(len(sent), 1)
        self.assertIn("удалён", sent[0])
        self.assertIn("2026-05-30", sent[0], "в алерте должна быть дата удаления")

    def test_missing_service_is_reported(self):
        pf, sent, _ = load_preflight(lambda q, v: {"data": {"service": None}})
        self.assertFalse(pf(DEAD_ID, "Силли"))
        self.assertIn("не найден", sent[0])

    def test_empty_variable_is_reported(self):
        pf, sent, _ = load_preflight(lambda q, v: {"data": {"service": None}})
        self.assertFalse(pf("", "Силли"))
        self.assertIn("не задан", sent[0])

    def test_live_service_is_silent(self):
        # Штатный старт не должен писать человеку — иначе алерт обесценится.
        pf, sent, log = load_preflight(
            lambda q, v: {"data": {"service": {"name": "ai-office-shared",
                                               "deletedAt": None}}})
        self.assertTrue(pf(DEAD_ID, "Силли"))
        self.assertEqual(sent, [])
        self.assertTrue(any("на месте" in x for x in log.lines))

    def test_api_outage_is_not_blamed_on_the_variable(self):
        # railway() отдаёт {} при любой сетевой беде. Это не улика против id.
        pf, sent, _ = load_preflight(lambda q, v: {})
        self.assertFalse(pf(DEAD_ID, "Силли"))
        self.assertIn("не смог проверить", sent[0])
        self.assertNotIn("удалён", sent[0])
        self.assertNotIn("не найден", sent[0])

    def test_transient_failure_is_retried(self):
        # Одна моргалка не должна поднимать человека среди ночи.
        calls = {"n": 0}

        def flaky(q, v):
            calls["n"] += 1
            if calls["n"] == 1:
                return {}
            return {"data": {"service": {"name": "ai-office-shared",
                                         "deletedAt": None}}}

        pf, sent, _ = load_preflight(flaky)
        self.assertTrue(pf(DEAD_ID, "Силли"))
        self.assertEqual(sent, [])
        self.assertEqual(calls["n"], 2)

    def test_retries_are_bounded(self):
        calls = {"n": 0}

        def always_down(q, v):
            calls["n"] += 1
            return {}

        pf, _, _ = load_preflight(always_down)
        pf(DEAD_ID, "Силли")
        self.assertLessEqual(calls["n"], 3, "ретраи должны быть с потолком")


class TestPreflightIsWired(unittest.TestCase):
    def test_main_calls_preflight_for_both_watched_services(self):
        # Функция без вызова — это документация, а не гейт.
        with open(SRC, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("preflight_service(SILLI_SERVICE_ID", src)
        self.assertIn("preflight_service(TRADER_SERVICE_ID", src)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
import logging
import unittest

from streamvault.events import EventBus
from streamvault.logs import BusLogHandler


class EventBusTest(unittest.TestCase):

    def test_failing_subscriber_does_not_block_others(self):
        bus = EventBus()
        received = []

        def broken(event):
            raise RuntimeError("сломался")

        bus.subscribe(broken)
        unsubscribe = bus.subscribe(received.append)
        with self.assertLogs(level="ERROR"):
            bus.emit("channel", id="twitch:x")
        self.assertEqual(received[0]["type"], "channel")
        self.assertEqual(received[0]["id"], "twitch:x")

        unsubscribe()
        with self.assertLogs(level="ERROR"):
            bus.emit("channel", id="twitch:y")
        self.assertEqual(len(received), 1)

    def test_log_forwarding_does_not_loop(self):
        # подписчик падает на каждом событии, его ошибка пишется в лог,
        # лог пересылается в шину — без защиты это шло бы по кругу
        bus = EventBus()
        calls = []

        def broken(event):
            calls.append(event["type"])
            raise RuntimeError("сломался")

        bus.subscribe(broken)
        logger = logging.getLogger()
        handler = BusLogHandler(bus)
        # assertLogs подменяет обработчики корневого логгера, поэтому свой
        # добавляется уже внутри
        with self.assertLogs(level="INFO") as captured:
            logger.addHandler(handler)
            try:
                logging.info("строка журнала")
            finally:
                logger.removeHandler(handler)
        self.assertEqual(calls, ["log"])
        self.assertTrue(any("подписчик" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()

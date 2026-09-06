import json
import unittest
import urllib.error
import urllib.request
from wechat_agent.delivery_broker import DeliveryBroker


class BrokerTests(unittest.TestCase):
    def test_authentication_and_task_ticket_validation(self):
        calls = []
        def deliver(ticket, path):
            if ticket != "active-test-ticket":
                raise ValueError("expired")
            calls.append(path)
            return {"ok": True, "status": "sent"}
        broker = DeliveryBroker(deliver)
        try:
            def request(token, ticket):
                return urllib.request.urlopen(urllib.request.Request(broker.url,
                    data=json.dumps({"task_ticket": ticket, "path": "D:/test.txt"}).encode(),
                    headers={"Authorization": "Bearer " + token}), timeout=2)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request("wrong", "active-test-ticket")
            self.assertEqual(403, caught.exception.code)
            caught.exception.close()
            with request(broker.token, "expired") as response:
                self.assertFalse(json.load(response)["ok"])
            with request(broker.token, "active-test-ticket") as response:
                self.assertTrue(json.load(response)["ok"])
            self.assertEqual(["D:/test.txt"], calls)
        finally:
            broker.close()

"""The dev simulator (the merchant's "try your assistant" tool) always reports the assistant's reply.

Found in live testing: the request commits the message to the durable inbox and then claims it, but a background
worker polling at that moment can claim it first. The simulator then answered {"status": "queued", "reply": null}
and the merchant saw no answer although the worker replied a few seconds later (and every following message of
that customer queued behind it). It now waits for that worker like WhatsApp would."""
import threading

import app.workflows.inbound as inbound
from tests.conftest import drain


def test_simulator_waits_for_the_worker_that_claimed_the_message(fashion, outbox, monkeypatch):
    real_claim = inbound.claim

    def worker_won_the_race(session_factory=inbound.SessionLocal, only_id=None):
        return None if only_id else real_claim(session_factory)

    monkeypatch.setattr(inbound, "claim", worker_won_the_race)
    worker = threading.Timer(0.5, drain)  # the background worker
    worker.start()
    r = fashion.post("/api/dev/simulate", json={"text": "black sneakers under 100k", "from_number": "250788999000"})
    worker.join()
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "replied" and body["conversation_id"] and body["agent_run_id"]
    assert body["reply"].startswith("Here's what I found")


def test_simulator_reply_when_processed_in_the_request(fashion, outbox):
    body = fashion.post("/api/dev/simulate", json={"text": "black sneakers under 100k"}).json()
    assert body["status"] == "replied" and body["reply"].startswith("Here's what I found")

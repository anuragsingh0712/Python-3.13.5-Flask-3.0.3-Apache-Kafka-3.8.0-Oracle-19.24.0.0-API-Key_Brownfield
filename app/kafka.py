"""Kafka event publisher used only by production wiring."""
from __future__ import annotations

import json
from typing import Any

from app.core import Settings
from app.repository import EventPublisher


class KafkaEventPublisher(EventPublisher):
    def __init__(self, settings: Settings) -> None:
        from kafka import KafkaProducer
        self._producer: Any = KafkaProducer(
            bootstrap_servers=settings.kafka_servers.split(","),
            client_id=settings.kafka_client_id,
            value_serializer=lambda value: json.dumps(value, default=str).encode("utf-8"),
        )

    def publish_after_commit(self, topic: str, payload: dict[str, Any]) -> None:
        self._producer.send(topic, payload)

    def close(self) -> None:
        self._producer.flush(timeout=10)
        self._producer.close(timeout=10)

import os
import json
import logging
import time
from datetime import datetime
from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable, KafkaError

logger = logging.getLogger(__name__)

class KafkaTelemetryProducer:
    def __init__(self, bootstrap_servers: str = "localhost:9092", dlq_filepath: str = "data/dead_letter_queue.log"):
        self.bootstrap_servers = bootstrap_servers
        self.dlq_filepath = dlq_filepath
        self.producer = None
        self.connected = False
        self.last_connect_time = 0.0
        
        # Ensure dead letter directory exists
        os.makedirs(os.path.dirname(self.dlq_filepath), exist_ok=True)
        
        # Attempt initial connection
        self.connect()

    def connect(self):
        """Attempts to connect to the Kafka broker."""
        self.last_connect_time = time.time()
        try:
            logger.info(f"Connecting to Kafka brokers at {self.bootstrap_servers}...")
            self.producer = KafkaProducer(
                bootstrap_servers=self.bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                # Fail fast on connection loss so we don't freeze the simulator loop
                request_timeout_ms=2000,
                max_block_ms=2000,
                retries=2
            )
            self.connected = True
            logger.info("Kafka Producer successfully initialized and connected.")
        except NoBrokersAvailable:
            self.connected = False
            self.producer = None
            logger.warning(f"Kafka brokers at {self.bootstrap_servers} are not available. Producer will run in fallback DLQ mode.")
        except Exception as e:
            self.connected = False
            self.producer = None
            logger.error(f"Failed to initialize Kafka producer: {e}. Running in fallback DLQ mode.")

    def _write_to_dlq(self, topic: str, payload: dict, error_message: str):
        """Writes the payload to a local dead-letter-queue log file as a fallback."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "topic": topic,
            "error": error_message,
            "payload": payload
        }
        try:
            with open(self.dlq_filepath, "a") as f:
                f.write(json.dumps(entry) + "\n")
            logger.warning(f"Telemetry backup logged locally to DLQ due to Kafka connectivity issue.")
        except Exception as e:
            logger.critical(f"FATAL: Could not write telemetry to local DLQ file: {e}")

    def publish(self, topic: str, payload: dict):
        """
        Publishes telemetry payload to the specified Kafka topic.
        If Kafka broker is unavailable, falls back to logging locally.
        """
        # If not connected, try a quick reconnect (throttled to once per 30 seconds)
        if not self.connected or self.producer is None:
            if time.time() - self.last_connect_time > 30:
                self.connect()

        if self.connected and self.producer is not None:
            try:
                # Asynchronous send
                future = self.producer.send(topic, value=payload)
                
                # We can add a success/failure callback if needed
                def on_send_success(record_metadata):
                    pass
                
                def on_send_error(excp):
                    logger.error(f"Error sending telemetry payload to {topic}: {excp}")
                    self._write_to_dlq(topic, payload, str(excp))
                
                future.add_callback(on_send_success)
                future.add_errback(on_send_error)
            except KafkaError as ke:
                logger.error(f"Kafka error encountered during send to {topic}: {ke}")
                self._write_to_dlq(topic, payload, str(ke))
            except Exception as e:
                logger.error(f"Unexpected error sending payload to {topic}: {e}")
                self._write_to_dlq(topic, payload, str(e))
        else:
            self._write_to_dlq(topic, payload, "Kafka broker connection unavailable.")

    def close(self):
        """Closes the Kafka producer connection."""
        if self.producer is not None:
            try:
                logger.info("Closing Kafka Producer connection...")
                self.producer.close(timeout=3)
                logger.info("Kafka Producer successfully closed.")
            except Exception as e:
                logger.warning(f"Error closing Kafka producer: {e}")
            finally:
                self.producer = None
                self.connected = False

if __name__ == "__main__":
    # Test class in DLQ fallback mode
    logging.basicConfig(level=logging.INFO)
    test_producer = KafkaTelemetryProducer(bootstrap_servers="localhost:9092")
    test_producer.publish("gridmind.telemetry.test", {"test": "value"})
    test_producer.close()

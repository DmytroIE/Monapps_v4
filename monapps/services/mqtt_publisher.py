import os
import logging
import paho.mqtt.client as mqtt

# from services.alarm_log import add_to_alarm_log

logger = logging.getLogger(__name__)


def on_connect(client: mqtt.Client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        # add_to_alarm_log(
        #     "INFO", "Connected to the broker", instance=client._client_id.decode("utf-8")
        # )
        logger.info(f"MQTT publisher {publisher_id} connected")
    else:
        # add_to_alarm_log(
        #     "ERROR",
        #     f"Failed to connect to the broker, reason code: {reason_code}",
        #     instance=client._client_id.decode("utf-8"),
        # )
        logger.error(f"MQTT publisher {publisher_id} failed to connect, reason code: {reason_code}")


def on_disconnect(client: mqtt.Client, userdata, flags, reason_code, properties):
    # add_to_alarm_log(
    #     "INFO", "Disconnected from the broker", instance=client._client_id.decode("utf-8")
    # )
    logger.info(f"MQTT publisher {publisher_id} disconnected")


# the publisher will be created only if MONAPP_PROC_NAME is set
proc_name = os.environ.get("MONAPP_PROC_NAME")
mqtt_publisher = None

if proc_name is not None:
    publisher_id = f"MQTT Pub {proc_name}"
    if len(publisher_id) > 22:
        publisher_id = publisher_id[:23]
    mqtt_publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=publisher_id, clean_session=True)
    mqtt_publisher.on_connect = on_connect
    mqtt_publisher.on_disconnect = on_disconnect
    try:
        mqtt_broker_host = os.getenv("MQTT_BROKER_HOST")
        if not mqtt_broker_host:
            raise ValueError("MQTT_BROKER_HOST env variable is not set")
        mqtt_publisher.connect(mqtt_broker_host, 1883, 60)
    except Exception as e:
        logger.error(f"MQTT publisher {publisher_id} failed to connect, reason: {e}")
        mqtt_publisher = None
    else:
        mqtt_publisher.loop_start()
        logger.info(f"MQTT publisher {publisher_id} created")

else:
    logger.info("MQTT publisher was not created")

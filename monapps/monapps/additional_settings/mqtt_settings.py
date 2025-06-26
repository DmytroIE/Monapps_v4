import os

# MQTT publisher settings
# this name will be included into the topic of published messages
INSTANCE_ID = os.environ.get("INSTANCE_ID", "some_instance")

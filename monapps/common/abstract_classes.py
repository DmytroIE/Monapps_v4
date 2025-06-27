import logging
import json
import humps

from django.db import models
from django.conf import settings

from common.constants import reeval_fields
from utils.db_field_utils import get_parent_full_id, get_instance_full_id
from utils.ts_utils import create_dt_from_ts_ms, create_now_ts_ms
from services.alarm_log import add_to_alarm_log
from utils.update_utils import enqueue_update, update_reeval_fields
from services.mqtt_publisher import mqtt_publisher

logger = logging.getLogger(__name__)


class PublishingOnSaveModel(models.Model):

    class Meta:
        abstract = True

    published_fields = set()
    name = "PublishingOnSaveModel instance"  # backup, if 'name' was forgotten to be defined in a subclass
    parent = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Update_fields is used to collect the names of the fields that were changed.
        # It will then be used in the 'save' method and reset.
        # To align with the Django 'save' method signature, this field should be
        # used explicitly in the 'save' method -> instance.save(update_fields=instance.update_fields)
        # However, this field is used in some auxiliary functions, so it is better to use it
        # than some arbitrary set.
        self.update_fields = set()

    # TODO: overload the 'delete' method as well

    def save(self, **kwargs):
        super().save(**kwargs)

        # 'update_fields' is used to collect the names of the fields that were changed.
        # It will then be used in the 'save' method and reset.
        # To align with the Django 'save' method signature, this field should be
        # used explicitly in the 'save' method -> instance.save(update_fields=instance.update_fields)
        # However, this field is used in some auxiliary functions, so it is better to use it
        # than some arbitrary set.
        # If 'update_fields' is None or its length > 0, it means that
        # there are some real changes in the saved instance (we assume that we save any model
        # only when some of its fields were changed, so the database is not hit for no reason).
        # Also, it is very important to publish on MQTT only when some fields of the model has changed
        # because the frontend app will also react when new publishing takes place
        update_fields = kwargs.get("update_fields")
        # if the result of 'kwargs.get("update_fields")' is None, it will substitute
        # both '"update_fields" not in kwargs' and 'kwargs["update_fields"] is None'
        if update_fields is None or len(update_fields) > 0:
            add_to_alarm_log("INFO", f"Something to publish, {update_fields=}", create_now_ts_ms(), instance=self)
            self.publish_on_mqtt()
            self.update_parent(update_fields)

        # reset after all the processing
        self.update_fields = set()

    def publish_on_mqtt(self):
        if mqtt_publisher is None or not mqtt_publisher.is_connected():
            return

        mqtt_pub_dict = self.create_mqtt_pub_dict()

        topic = f"procdata/{settings.INSTANCE_ID}/{self._meta.model_name}/{self.pk}"
        payload_str = json.dumps(mqtt_pub_dict)
        mess_info = mqtt_publisher.publish(topic, payload_str, qos=0, retain=True)
        add_to_alarm_log("INFO", "Changes published", create_now_ts_ms(), instance=self)

    def create_mqtt_pub_dict(self) -> dict:
        mqtt_pub_dict = {}
        mqtt_pub_dict["id"] = get_instance_full_id(self)
        mqtt_pub_dict["name"] = self.name
        mqtt_pub_dict["parentId"] = get_parent_full_id(self)

        for field in self.published_fields:
            attr = getattr(self, field, "NO_ATTR")
            if attr == "NO_ATTR":
                logger.warning(f"No attribute {field} in {self} to publish")
                continue
            camelized_field = humps.camelize(field)
            mqtt_pub_dict[camelized_field] = attr

        return mqtt_pub_dict

    def update_parent(self, update_fields):
        if self.parent is None:
            return

        if update_fields is None:
            # it happens usually when the instance is saved in the admin console
            # in this case, updated all three fields of the parent
            parent_reeval_fields = reeval_fields
        else:
            parent_reeval_fields = reeval_fields.intersection(update_fields)
        if len(parent_reeval_fields) == 0:
            return

        if update_reeval_fields(self.parent, parent_reeval_fields):
            print(f"!!!parent needs to be reevaluated: {parent_reeval_fields}")
            if enqueue_update(self.parent, create_now_ts_ms()):
                print("!!!parent update was not previously enqueued")
                self.parent.save(update_fields=self.parent.update_fields)


class AnyDsReading(models.Model):
    class Meta:
        abstract = True

    short_name = ""

    pk = models.CompositePrimaryKey("datastream_id", "time")
    time = models.BigIntegerField()
    datastream = models.ForeignKey("datastreams.Datastream", on_delete=models.PROTECT)
    db_value = models.FloatField()

    @property
    def value(self) -> float | int:
        if self.datastream.is_value_interger:
            return round(self.db_value)
        else:
            return self.db_value

    @value.setter
    def value(self, value: float) -> None:
        self.db_value = value

    def __str__(self):
        dt_str = create_dt_from_ts_ms(self.time).strftime("%Y/%m/%d %H:%M:%S")
        return f"{self.short_name} ds:{self.datastream.pk} ts:{dt_str} val: {self.value}"


class AnyNoDataMarker(models.Model):

    class Meta:
        abstract = True

    short_name = ""

    pk = models.CompositePrimaryKey("datastream_id", "time")
    time = models.BigIntegerField()
    datastream = models.ForeignKey("datastreams.Datastream", on_delete=models.PROTECT)

    def __str__(self):
        dt_str = create_dt_from_ts_ms(self.time).strftime("%Y/%m/%d %H:%M:%S")
        return f"{self.short_name} ds:{self.datastream.pk} ts:{dt_str}"

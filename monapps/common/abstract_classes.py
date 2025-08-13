import logging
import json
import humps

from django.db import models
from django.conf import settings

from common.constants import reeval_fields
from utils.db_field_utils import get_parent_full_id, get_instance_full_id
from utils.ts_utils import create_dt_from_ts_ms, create_now_ts_ms
# from services.alarm_log import add_to_alarm_log
from utils.update_utils import enqueue_update, update_reeval_fields
from services.mqtt_publisher import mqtt_publisher

logger = logging.getLogger("#abs_classes")


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
        logger.debug(f"<{get_instance_full_id(self)}>: Saving")
        # 'update_fields' is used to collect the names of the fields that were changed.
        # It will then be used in the 'save' method and reset.
        # To align with the Django 'save' method signature, this field should be
        # used explicitly in the 'save' method -> instance.save(update_fields=instance.update_fields).
        # Sure, it is possible to use any set variable, but this built-in field can "collect"
        # changes while the instance is going through many changing functions.
        # Also, some auxiliary functions count on this field, so it is recommended to use it.
        # If the length of 'update_fields' is greater than 0 by this point, it means that
        # some real changes in the saved instance (we assume that we save any model
        # only when some of its fields were changed, so the database is not hit for no reason).
        # In this case, it is responsibility of the caller to enqueue the update of the parent.
        # If 'update_fields' is 'None', it most likely that the instance was saved
        # in the admin console. In this case, the parent update with all reeval fields
        # will be enqueued here. Therefore, don't save the paren in the code without
        # explicit 'update_fields' parameter.
        update_fields = kwargs.get("update_fields")
        logger.debug(f"<{get_instance_full_id(self)}>: update_fields: {update_fields}")

        if update_fields is None or len(update_fields) > 0:
            self.publish_on_mqtt(update_fields)
        if update_fields is None:
            self.update_parent_at_bulk_save()

        # reset after all the processing
        self.update_fields = set()

    def publish_on_mqtt(self, update_fields):
        if mqtt_publisher is None or not mqtt_publisher.is_connected():
            return

        # check if at least one of 'update_fields' is in list of fields to publish
        if update_fields is not None and len(self.published_fields.intersection(update_fields)) == 0:
            return

        mqtt_pub_dict = self.create_mqtt_pub_dict()

        topic = f"procdata/{settings.MONAPP_INSTANCE_ID}/{self._meta.model_name}/{self.pk}"
        payload_str = json.dumps(mqtt_pub_dict)
        mqtt_publisher.publish(topic, payload_str, qos=0, retain=True)
        # add_to_alarm_log("INFO", "Changes published", instance=self)
        logger.info(f"<{get_instance_full_id(self)}>: Changes published on MQTT")

    def create_mqtt_pub_dict(self) -> dict:
        mqtt_pub_dict = {}
        mqtt_pub_dict["id"] = get_instance_full_id(self)
        mqtt_pub_dict["name"] = self.name
        mqtt_pub_dict["parentId"] = get_parent_full_id(self)

        for field in self.published_fields:
            attr = getattr(self, field, "NO_ATTR")
            if attr == "NO_ATTR":
                logger.warning(f"<{get_instance_full_id(self)}>: No attribute {field} to publish")
                continue
            camelized_field = humps.camelize(field)
            mqtt_pub_dict[camelized_field] = attr

        return mqtt_pub_dict

    def update_parent_at_bulk_save(self):
        if self.parent is None:
            return

        logger.debug(f"<{get_instance_full_id(self)}>: Updating parent from the bulk 'save' method")
        if hasattr(self.parent, "reeval_fields"):
            update_reeval_fields(self.parent, reeval_fields)
            logger.debug(f"<{get_instance_full_id(self)}>: To be reevaluated: {reeval_fields}")
        if hasattr(self.parent, "next_upd_ts"):
            enqueue_update(self.parent, create_now_ts_ms(), coef=0.2)
            logger.debug(f"<{get_instance_full_id(self)}>: Update enqueued for {self.parent.next_upd_ts}")
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
            return int(self.db_value)
        else:
            return self.db_value

    @value.setter
    def value(self, value: float) -> None:
        if self.datastream.is_value_interger:
            self.db_value = round(value, 0)
        else:
            self.db_value = value

    def __str__(self):
        dt_str = create_dt_from_ts_ms(self.time).strftime("%Y/%m/%d %H:%M:%S")
        if self.datastream.is_value_interger:
            return f"{self.short_name} ds:{self.datastream.pk} ts:{dt_str} val: {self.value}"
        else:
            return f"{self.short_name} ds:{self.datastream.pk} ts:{dt_str} val: {self.value:.3f}"


class AnyNoDataMarker(models.Model):

    class Meta:
        abstract = True

    short_name = ""

    pk = models.CompositePrimaryKey("datastream_id", "time")
    time = models.BigIntegerField()
    datastream = models.ForeignKey("datastreams.Datastream", on_delete=models.PROTECT)

    def __str__(self):
        dt_str = create_dt_from_ts_ms(self.time).strftime("%Y/%m/%d %H:%M:%S.%f")
        return f"{self.short_name} ds:{self.datastream.pk} ts:{dt_str[:-3]}"

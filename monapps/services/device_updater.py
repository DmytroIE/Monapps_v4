from django.db import transaction
from django.conf import settings

from apps.devices.models import Device
from utils.ts_utils import create_now_ts_ms
from utils.update_utils import (
    derive_health_from_children,
    enqueue_update,
    update_reeval_fields,
    set_attr_if_cond
)


class DeviceUpdater:
    def __init__(self):
        self.now_ts = create_now_ts_ms()
        self.parent_map = {}

    @transaction.atomic
    def execute(self):
        device_qs = (
            Device.objects.filter(
                next_upd_ts__lte=self.now_ts,
            )
            .order_by("next_upd_ts")
            .prefetch_related("parent")
            .prefetch_related("datastreams")
            .select_for_update()[: settings.MAX_DEVICES_TO_UPD]
        )

        for dev in device_qs:
            self.update_device(dev)

        for parent in self.parent_map.values():
            parent.save(update_fields=parent.update_fields)

    def update_device(self, dev):
        parent = dev.parent
        if parent is not None:
            # FIXME: should this code be used
            if parent.name not in self.parent_map:
                self.parent_map[parent.name] = parent
            # or this?
            # Doesn't the latter replace the parent
            # (so resets 'update_fields' and 'reeval_fields')?
            # self.parent_map[parent.name] = parent

        children = list(dev.datastreams.filter(is_enabled=True))

        # evaluate health
        self.update_device_health(dev, children, parent)

        dev.next_upd_ts = settings.MAX_TS_MS
        dev.update_fields.add("next_upd_ts")

        dev.save(update_fields=dev.update_fields)

    def update_device_health(self, dev, children, parent):
        chld_health = derive_health_from_children(children)
        set_attr_if_cond(chld_health, "!=", dev, "chld_health")
        health = max(dev.msg_health, dev.chld_health)

        if not set_attr_if_cond(health, "!=", dev, "health"):
            return

        enqueue_update(parent, self.now_ts)
        update_reeval_fields(parent, "health")

from django.db import transaction
from django.conf import settings

from apps.datastreams.models import Datastream
from common.constants import HealthGrades
from utils.ts_utils import create_now_ts_ms
from utils.update_utils import enqueue_update, set_attr_if_cond


class PeriodicDsHealthUpdater:
    def __init__(self):
        self.now_ts = create_now_ts_ms()
        self.dev_map = {}

    @transaction.atomic
    def execute(self):

        # bring MAX_DS_TO_HEALTH_PROC active periodic datastream instances, the rest will be processed further
        ds_qs = (
            Datastream.objects.filter(
                health_next_eval_ts__lte=self.now_ts,
            )
            .filter(is_enabled=True)
            .exclude(time_update__isnull=True)
            .order_by("health_next_eval_ts")
            .prefetch_related('parent')
            .select_for_update()[: settings.MAX_DS_TO_HEALTH_PROC]
        )

        for ds in ds_qs:
            self.update_ds(ds)

        for dev in self.dev_map.values():
            dev.save(update_fields=dev.update_fields)

    def update_ds(self, ds):
        dev = ds.parent

        # FIXME: should this code be used
        if dev.dev_ui not in self.dev_map:
            self.dev_map[dev.dev_ui] = dev
        # or this?
        # Doesn't the latter replace the dev
        # (so resets 'update_fields')?
        # self.dev_map[dev.dev_ui] = dev

        self.update_health(ds, dev)

        ds.health_next_eval_ts = self.now_ts + max(
            settings.TIME_DS_HEALTH_EVAL_MS, ds.t_update * settings.NEXT_EVAL_MARGIN_COEF
        )
        ds.update_fields.add('health_next_eval_ts')

        ds.save(update_fields=ds.update_fields)

    def update_health(self, ds, dev):
        if ds.last_reading_ts is None:
            if self.now_ts - ds.created_ts > ds.time_nd_health_error:  # TODO: from 'enabled' not from 'created'?
                nd_health = HealthGrades.ERROR
            else:
                nd_health = HealthGrades.UNDEFINED
        else:
            if self.now_ts - ds.last_reading_ts > ds.time_nd_health_error:
                nd_health = HealthGrades.ERROR
            else:
                nd_health = HealthGrades.OK
        if not set_attr_if_cond(nd_health, "!=", ds, "nd_health"):
            return

        health = max(ds.msg_health, ds.nd_health)
        if not set_attr_if_cond(health, "!=", ds, "health"):
            return

        enqueue_update(dev, self.now_ts)

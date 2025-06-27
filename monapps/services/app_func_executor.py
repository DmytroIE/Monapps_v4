import logging
from collections.abc import Iterable
from typing import Literal
from django.db import transaction, IntegrityError
from django_celery_beat.models import PeriodicTask

from apps.applications.models import Application
from apps.dfreadings.models import DfReading
from common.constants import HealthGrades, STATUS_FIELD_NAME, CURR_STATE_FIELD_NAME, reeval_fields
from common.complex_types import AppFunction
from utils.ts_utils import create_now_ts_ms
from utils.sequnce_utils import find_instance_with_max_attr
from utils.alarm_utils import update_alarm_map
from utils.update_utils import enqueue_update, update_reeval_fields, set_attr_if_cond
from services.alarm_log import add_to_alarm_log

logger = logging.getLogger(__name__)


class AppFuncExecutor:
    def __init__(self, app: Application, app_func: AppFunction, task: PeriodicTask):
        self.app = app
        self.task = task
        self.app_func = app_func
        self.now_ts = create_now_ts_ms()
        self.update_map = {}
        self.excep_health = HealthGrades.UNDEFINED
        self.health_from_app = HealthGrades.UNDEFINED
        self.cs_health = HealthGrades.UNDEFINED  # health based on the cursor timestamp

    @transaction.atomic
    def execute(self):
        self.app = Application.objects.select_for_update().get(pk=self.app.pk)
        self.task = PeriodicTask.objects.select_for_update().get(pk=self.task.pk)
        if self.app.is_enabled:
            try:
                logger.info(f"Starting app function, {self.app}")
                self.run_exec_routine()
                logger.info(f"App function was executed, {self.app}")
            except IntegrityError:
                logger.error("An attempt to rewrite existing df readings detected")
                self.excep_health = HealthGrades.ERROR
            except Exception as e:
                self.excep_health = HealthGrades.ERROR
                logger.error(f"Error happened while executing app function, {e}")
        self.now_ts = create_now_ts_ms()  # update 'now_ts' as app func execution can take much time
        self.run_post_exec_routine()

    @transaction.atomic  # lock datafeeds
    def run_exec_routine(self):
        native_df_qs = self.app.get_native_df_qs().select_for_update()
        native_df_map = {df.name: df for df in native_df_qs}
        derived_df_qs = self.app.get_derived_df_qs().select_for_update()
        derived_df_map = {df.name: df for df in derived_df_qs}
        self.task = PeriodicTask.objects.select_for_update().get(pk=self.task.pk)

        derived_df_readings, self.update_map = self.app_func(self.app, native_df_map, derived_df_map)

        for df_row in derived_df_readings.values():
            df = df_row["df"]
            new_df_readings = df_row["new_df_readings"]
            latest_dfr = self.save_new_df_readings(new_df_readings)
            if latest_dfr is not None:
                self.update_datafeed(df, latest_dfr)
                if df.name == STATUS_FIELD_NAME and self.app.type.has_status:
                    self.assign_new_cs_st_value(latest_dfr, "status")
                if df.name == CURR_STATE_FIELD_NAME and self.app.type.has_curr_state:
                    self.assign_new_cs_st_value(latest_dfr, "curr_state")

        self.update_catching_up()
        self.update_cursor_pos()
        self.update_alarms()

    def save_new_df_readings(self, new_df_readings):
        latest_dfr = find_instance_with_max_attr(new_df_readings)
        if latest_dfr is not None:  # the same as 'if len(new_df_readings) > 0'
            DfReading.objects.bulk_create(new_df_readings)
        return latest_dfr

    def update_datafeed(self, df, latest_dfr):
        max_rts = latest_dfr.time
        if set_attr_if_cond(max_rts, ">", df, "last_reading_ts"):
            df.save(update_fields=df.update_fields)

    def assign_new_cs_st_value(self, latest_dfr, name: Literal["status", "curr_state"]):
        if not set_attr_if_cond(latest_dfr.time, ">", self.app, f"last_{name}_update_ts"):
            return
        if not set_attr_if_cond(latest_dfr.value, "!=", self.app, name):
            return
        full_name = "Current state" if name == "curr_state" else "Status"
        add_to_alarm_log("INFO", f"{full_name} changed", latest_dfr.time, instance=self.app)

    def update_catching_up(self):
        if (is_catching_up := self.update_map.get("is_catching_up")) is None:
            return

        set_attr_if_cond(is_catching_up, "!=", self.app, "is_catching_up")

        if is_catching_up and not self.app.is_catching_up:
            self.task.interval = self.app.catch_up_interval
            self.task.save()
        elif not is_catching_up and self.app.is_catching_up:
            self.task.interval = self.app.invoc_interval
            self.task.save()

    def update_cursor_pos(self):
        if (ts := self.update_map.get("cursor_ts")) is None:
            return
        cursor_ts = ts
        set_attr_if_cond(cursor_ts, ">", self.app, "cursor_ts")

    def update_alarms(self):
        if (alarm_payload := self.update_map.get("alarm_payload")) is None:
            return
        for ts, row in alarm_payload.items():
            error_dict = row.get("e")
            upd_error_map, _ = update_alarm_map(self.app, error_dict, ts, "errors")
            set_attr_if_cond(upd_error_map, "!=", self.app, "errors")

            warning_dict = row.get("w")
            upd_warning_map, _ = update_alarm_map(self.app, warning_dict, ts, "warnings")
            set_attr_if_cond(upd_warning_map, "!=", self.app, "warnings")

            app_infos_for_ts = row.get("i")
            if app_infos_for_ts is not None and isinstance(app_infos_for_ts, Iterable):
                for info_str in app_infos_for_ts:
                    add_to_alarm_log("INFO", info_str, ts, self.app)

    def run_post_exec_routine(self):
        self.update_staleness("status")
        self.update_staleness("curr_state")
        self.update_health()
        app_update_fields = self.app.update_fields.copy()  # copy, as after 'app.save' its 'update_fields' will be reset
        self.app.save(update_fields=self.app.update_fields)
        self.update_parent(app_update_fields)

    def update_staleness(self, name: Literal["status", "curr_state"]):
        has = getattr(self.app.type, f"has_{name}", False)
        if not has:
            return
        last_update_ts = getattr(self.app, f"last_{name}_update_ts")
        time_stale = getattr(self.app, f"time_{name}_stale")
        if last_update_ts is not None:
            is_stale = self.now_ts - last_update_ts > time_stale
        else:
            is_stale = self.now_ts - self.app.created_ts > time_stale

        if set_attr_if_cond(is_stale, "!=", self.app, f"is_{name}_stale"):
            if is_stale:
                full_name = "Current state" if name == "curr_state" else "Status"
                add_to_alarm_log("INFO", f"{full_name} is stale", self.now_ts, instance=self.app)

    def eval_health_from_app(self):
        if (h := self.update_map.get("health")) is not None:
            # HealthGrades.OK is not used for this type of health
            self.health_from_app = h if h != HealthGrades.OK else HealthGrades.UNDEFINED

    def eval_cs_health(self):
        # health based on the cursor timestamp
        if self.app.is_enabled and not self.app.is_catching_up:
            if self.now_ts - self.app.cursor_ts > self.app.time_health_error:
                self.cs_health = HealthGrades.ERROR
            else:
                self.cs_health = HealthGrades.OK

    def update_health(self):
        self.eval_health_from_app
        self.eval_cs_health()
        health = max(self.cs_health, self.health_from_app, self.excep_health)
        if set_attr_if_cond(health, "!=", self.app, "health"):
            add_to_alarm_log("INFO", "Health changed", self.now_ts, instance=self.app)

    def update_parent(self, app_update_fields):
        parent = self.app.parent
        if parent is None:
            return

        parent_reeval_fields = reeval_fields.intersection(app_update_fields)
        if "is_status_stale" in app_update_fields:
            parent_reeval_fields.add("status")
        if "is_curr_state_stale" in app_update_fields:
            parent_reeval_fields.add("curr_state")
        if len(parent_reeval_fields) == 0:
            return

        if update_reeval_fields(parent, parent_reeval_fields):
            enqueue_update(parent, self.now_ts)

        # parent will not be saved if 'parent.update_fields' is empty
        parent.save(update_fields=parent.update_fields)

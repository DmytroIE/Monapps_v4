import logging
import traceback
from collections.abc import Iterable
from typing import Literal
from django.db import transaction, IntegrityError
from django.conf import settings
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
from services.app_log import add_to_app_log

logger = logging.getLogger("#app_func_executor")


class AppFuncExecutor:
    def __init__(self, app: Application, app_func: AppFunction, task: PeriodicTask):
        self.app = app
        self.task = task
        self.app_func = app_func
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
                logger.debug("Starting app function")
                self.run_exec_routine()
                logger.debug("App function was executed")
            except IntegrityError:
                logger.error("An attempt to rewrite existing df readings detected")
                self.excep_health = HealthGrades.ERROR
            except Exception:
                self.excep_health = HealthGrades.ERROR
                logger.error(f"Error happened while executing app function, {traceback.format_exc(-1)}")

        logger.debug("Update other parameters")
        self.run_post_exec_routine()

    @transaction.atomic  # lock datafeeds
    def run_exec_routine(self):
        native_df_qs = self.app.get_native_df_qs().select_for_update()
        native_df_map = {df.name: df for df in native_df_qs}
        derived_df_qs = self.app.get_derived_df_qs().select_for_update()
        derived_df_map = {df.name: df for df in derived_df_qs}

        min_ts = self.find_min_dfr_ts(derived_df_qs)
        if min_ts == settings.MAX_TS_MS:
            logger.debug("No df readings to process, return...")
            return
        
        # protection from forgetting to set proper value for app.cursor_ts
        if self.app.cursor_ts < min_ts:
            logger.debug(f"Cursor ts {self.app.cursor_ts} is < min_ts {min_ts}")
            self.app.cursor_ts = min_ts
            logger.debug(f"Cursor ts upgraded")

        self.task = PeriodicTask.objects.select_for_update().get(pk=self.task.pk)

        derived_df_readings, self.update_map = self.app_func(self.app, native_df_map, derived_df_map)

        self.update_catching_up()

        for df_row in derived_df_readings.values():
            df = df_row["df"]
            new_df_readings = df_row["new_df_readings"]
            latest_dfr = self.save_new_df_readings(new_df_readings)
            if latest_dfr is not None:
                self.update_datafeed(df, latest_dfr)
                if df.name == STATUS_FIELD_NAME:
                    self.assign_new_cs_st_value(latest_dfr, "status")
                if df.name == CURR_STATE_FIELD_NAME:
                    self.assign_new_cs_st_value(latest_dfr, "curr_state")

        self.update_cursor_pos()
        self.update_alarms()
        self.update_state()
    
    def find_min_dfr_ts(self, native_df_qs) -> int:
        # protection from forgetting to set proper value for app.cursor_ts
        min_ts = settings.MAX_TS_MS
        for df in native_df_qs:
            first_dfr = DfReading.objects.filter(datafeed__id=df.pk).order_by("time").first()
            if first_dfr is None:
                continue
            if first_dfr.time < min_ts:
                min_ts = first_dfr.time
        return min_ts

    def save_new_df_readings(self, new_df_readings):
        latest_dfr = find_instance_with_max_attr(new_df_readings)
        if latest_dfr is not None:  # the same as 'if len(new_df_readings) > 0'
            DfReading.objects.bulk_create(new_df_readings)
            logger.debug("New df readings were saved")
        return latest_dfr

    def update_datafeed(self, df, latest_dfr):
        max_rts = latest_dfr.time
        if set_attr_if_cond(max_rts, ">", df, "last_reading_ts"):
            df.save(update_fields=df.update_fields)
            # logger.debug(f"Datafeed '{df.name}' was updated")

    def assign_new_cs_st_value(self, latest_dfr, name: Literal["status", "curr_state"]):

        # when catching up, do not update status or curr_state, leave them frozen
        # it will help to avoid hitting parent assets too often
        if self.app.is_catching_up:
            return

        if not set_attr_if_cond(latest_dfr.time, ">", self.app, f"last_{name}_update_ts"):
            return
        if not set_attr_if_cond(latest_dfr.value, "!=", self.app, name):
            return
        full_name = CURR_STATE_FIELD_NAME if name == "curr_state" else STATUS_FIELD_NAME
        add_to_alarm_log("INFO", f"{full_name} changed", instance=self.app)
        logger.debug(f"{full_name} changed -> : {latest_dfr.value}")

    def update_catching_up(self):
        if (is_catching_up := self.update_map.get("is_catching_up")) is None:
            return

        if not set_attr_if_cond(is_catching_up, "!=", self.app, "is_catching_up"):
            return

        if is_catching_up:
            self.task.interval = self.app.catch_up_interval
            self.task.save()
            logger.debug("Catching up started")
        elif not is_catching_up:
            self.task.interval = self.app.invoc_interval
            self.task.save()
            logger.debug("Catching up finished")

    def update_cursor_pos(self):
        if (ts := self.update_map.get("cursor_ts")) is None:
            return
        cursor_ts = ts
        if set_attr_if_cond(cursor_ts, ">", self.app, "cursor_ts"):
            logger.debug(f"Cursor position was updated -> {cursor_ts}")

    def update_alarms(self):
        if (alarm_payload := self.update_map.get("alarm_payload")) is None:
            return
        for ts, row in alarm_payload.items():
            error_dict = row.get("e")
            upd_error_map, _ = update_alarm_map(
                self.app, error_dict, ts, "errors", add_to_log=add_to_app_log
            )
            set_attr_if_cond(upd_error_map, "!=", self.app, "errors")

            warning_dict = row.get("w")
            upd_warning_map, _ = update_alarm_map(
                self.app, warning_dict, ts, "warnings", add_to_log=add_to_app_log
            )
            set_attr_if_cond(upd_warning_map, "!=", self.app, "warnings")

            app_infos_for_ts = row.get("i")
            if app_infos_for_ts is not None and isinstance(app_infos_for_ts, Iterable):
                for info_str in app_infos_for_ts:
                    add_to_app_log("INFO", info_str, ts=ts, instance=self.app)

    def update_state(self):
        if (state := self.update_map.get("state")) is None:
            return
        set_attr_if_cond(state, "!=", self.app, "state")

    def run_post_exec_routine(self):
        self.update_staleness("status")
        self.update_staleness("curr_state")
        self.update_health()
        app_update_fields = self.app.update_fields.copy()  # copy, as after 'app.save' its 'update_fields' will be reset
        self.app.save(update_fields=self.app.update_fields)
        self.update_parent(app_update_fields)

    def update_staleness(self, name: Literal["status", "curr_state"]):

        # when catching up, do not update status or curr_state, leave them frozen
        # it will help to avoid hitting parent assets too often
        if self.app.is_catching_up:
            return

        full_name = CURR_STATE_FIELD_NAME if name == "curr_state" else STATUS_FIELD_NAME
        has = filter(lambda df: df.name == full_name, self.app.datafeeds.all())
        if not has:
            return
        last_update_ts = getattr(self.app, f"last_{name}_update_ts")
        time_stale = getattr(self.app, f"time_{name}_stale")
        now_ts = create_now_ts_ms()
        if last_update_ts is not None:
            is_stale = now_ts - last_update_ts > time_stale
        else:
            is_stale = now_ts - self.app.created_ts > time_stale

        if set_attr_if_cond(is_stale, "!=", self.app, f"is_{name}_stale"):
            if is_stale:
                add_to_alarm_log("INFO", f"{full_name} is stale", instance=self.app)
                logger.debug(f"{full_name} is stale")
            else:
                add_to_alarm_log("INFO", f"{full_name} is not stale", instance=self.app)
                logger.debug(f"{full_name} is not stale")

    def eval_health_from_app(self):
        if (h := self.update_map.get("health")) is not None:
            # HealthGrades.OK is not used for this type of health
            self.health_from_app = h if h != HealthGrades.OK else HealthGrades.UNDEFINED

    def eval_cs_health(self):
        # health based on the cursor timestamp
        now_ts = create_now_ts_ms()
        if self.app.is_enabled and not self.app.is_catching_up:
            if now_ts - self.app.cursor_ts > self.app.time_health_error:
                self.cs_health = HealthGrades.ERROR
            else:
                self.cs_health = HealthGrades.OK

    def update_health(self):

        # when catching up, do not update health
        if self.app.is_catching_up:
            return

        self.eval_health_from_app()
        self.eval_cs_health()

        health = max(self.cs_health, self.health_from_app, self.excep_health)

        if set_attr_if_cond(health, "!=", self.app, "health"):
            add_to_alarm_log("INFO", "Health changed", instance=self.app)
            logger.debug(f"Health changed -> {health}")

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

        now_ts = create_now_ts_ms()
        update_reeval_fields(parent, parent_reeval_fields)
        if len(parent.reeval_fields) == 0:
            return
        logger.debug(f"To be reevaluated: {parent.reeval_fields}")
        enqueue_update(parent, now_ts, coef=0.2)
        logger.debug(f"Parent update enqueued for {parent.next_upd_ts}")

        parent.save(update_fields=parent.update_fields)

from django.db import transaction
from django.conf import settings

from apps.assets.models import Asset
from utils.ts_utils import create_now_ts_ms
from utils.update_utils import update_func_by_property_map, enqueue_update, update_reeval_fields, set_attr_if_cond


class AssetUpdater:
    def __init__(self):
        self.now_ts = create_now_ts_ms()
        self.parent_map = {}

    @transaction.atomic
    def execute(self):
        asset_qs = (
            Asset.objects.filter(
                next_upd_ts__lte=self.now_ts,
            )
            .order_by("next_upd_ts")
            .prefetch_related("parent")
            .prefetch_related("assets")
            .prefetch_related("applications")
            .prefetch_related("devices")
            .select_for_update()[: settings.MAX_ASSETS_TO_UPD]
        )
        for asset in asset_qs:
            print(f"\n!!!!!!!{self.now_ts=}\nUpdating asset {asset.name}, reeval fields: {asset.reeval_fields}")
            self.update_asset(asset)

        for parent in self.parent_map.values():
            parent.save(update_fields=parent.update_fields)

    def update_asset(self, asset):
        if len(asset.reeval_fields) == 0:
            return

        parent = asset.parent
        if parent is not None:
            # FIXME: should this code be used
            if parent.name not in self.parent_map:
                self.parent_map[parent.name] = parent
            # or this?
            # Doesn't the latter replace the parent
            # (so resets 'update_fields' and 'reeval_fields')?
            # self.parent_map[parent.name] = parent

        children = [
            *asset.applications.all(),
            *asset.devices.all(),
            *asset.assets.all(),
        ]

        for field_name in asset.reeval_fields:
            self.update_asset_field(asset, parent, field_name, children)

        asset.reeval_fields = []  # reset
        asset.update_fields.add("reeval_fields")

        asset.next_upd_ts = settings.MAX_TS_MS  # move the update time to the Infinity
        asset.update_fields.add("next_upd_ts")

        asset.save(update_fields=asset.update_fields)

    def update_asset_field(self, asset, parent, field_name, children):
        func = update_func_by_property_map[field_name]
        new_value = func(children)

        if not set_attr_if_cond(new_value, "!=", asset, field_name):
            return

        if field_name == "status":
            asset.last_status_update_ts = self.now_ts
            asset.update_fields.add("last_status_update_ts")
        if field_name == "curr_state":
            asset.last_curr_state_update_ts = self.now_ts
            asset.update_fields.add("last_curr_state_update_ts")

        enqueue_update(parent, self.now_ts)
        update_reeval_fields(parent, field_name)

        if parent is not None:
            print(f"\n!!!!!{self.now_ts=}\nEnqueued parent update {parent.name} with {parent.reeval_fields}, {parent.next_upd_ts}\n")

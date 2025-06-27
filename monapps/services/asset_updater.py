from django.db import transaction
from django.conf import settings

from apps.assets.models import Asset
from utils.ts_utils import create_now_ts_ms
from utils.update_utils import update_func_by_property_map, enqueue_update, update_reeval_fields, set_attr_if_cond
from utils.db_field_utils import get_instance_full_id, get_parent_full_id


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

        asset_map = {}
        for asset in asset_qs:
            asset_full_id = get_instance_full_id(asset)
            if asset_full_id not in asset_map:
                asset_map[asset_full_id] = asset

        print(f"---asset_and_parent_map: {asset_map}")

        tree = self.create_asset_tree(asset_map)
        print(f"---tree: {tree}")
        self.procees_starting_from_leaves(tree)

    def create_asset_tree(self, asset_map):
        tree = []
        print("---create tree")
        for asset in asset_map.values():
            print(f"---asset: {asset}")
            if asset.parent is not None:
                print("Parent is not None")
                if get_instance_full_id(asset.parent) not in asset_map:
                    # for 'root' assets (that are not in the map) update will not happen in this iteration, but will be enqueued
                    asset.parent.root = True
                    tree.append(asset.parent)
                    print(f"---parent {asset.parent} added to the tree")

                asset.root = False
                if hasattr(asset.parent, "children"):
                    asset.parent.children.append(asset)
                else:
                    asset.parent.children = [asset]

                print(f"---asset {asset} added as children to the tree")
            else:
                asset.root = False
                tree.append(asset)
                print(f"---asset {asset} added to the tree")

        return tree

    def procees_starting_from_leaves(self, nodes):
        for node in nodes:
            if hasattr(node, "children") and len(node.children) > 0:
                self.procees_starting_from_leaves(node.children)
            if node.root:
                self.update_root_node(node)
            else:
                self.update_node(node)

    def update_root_node(self, asset):
        if "reeval_fields" in asset.update_fields:
            enqueue_update(asset, self.now_ts)
            asset.save(update_fields=asset.update_fields)

    def update_node(self, asset):
        children = [
            *asset.applications.all(),
            *asset.devices.all(),
            *asset.assets.all(),
        ]

        for field_name in asset.reeval_fields:
            self.update_asset_field(asset, field_name, children)

        asset.reeval_fields = []  # reset
        asset.update_fields.add("reeval_fields")

        asset.next_upd_ts = settings.MAX_TS_MS  # move the update time to the Infinity
        asset.update_fields.add("next_upd_ts")

        asset.save(update_fields=asset.update_fields)

    def update_asset_field(self, asset, field_name, children):
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

        update_reeval_fields(asset.parent, field_name)

import logging
from celery import shared_task

from services.asset_updater import AssetUpdater

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="update.assets")
def update_assets(self):
    AssetUpdater().execute()

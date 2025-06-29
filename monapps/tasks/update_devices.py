import logging
from celery import shared_task
from services.device_updater import DeviceUpdater

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="update.devices")
def update_devices(self):
    DeviceUpdater().execute()

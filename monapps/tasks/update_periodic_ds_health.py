import logging
from celery import shared_task
from services.periodic_ds_health_updater import PeriodicDsHealthUpdater

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="update.periodic_ds_health")
def update_periodic_ds_health(self):
    PeriodicDsHealthUpdater().execute()

import logging


class WorkerVerboseFilter(logging.Filter):

    def filter(self, record):
        # if "strategy" in record.pathname or "base" in record.pathname or "trace" in record.pathname:
        #     return False
        # return True
        if "base" in record.pathname or "received" in record.msg or "succeeded" in record.msg:
            return False
        return True

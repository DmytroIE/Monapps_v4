import logging


class OnlyLocalModulesFilter(logging.Filter):
    # to pass 'DEBUG' messages only from localmodules
    def filter(self, record):
        return record.levelno != logging.DEBUG or record.name.startswith("#")

import logging

modules_with_info_supressed = ["strategy", "trace"]


class OnlyLocalModulesFilter(logging.Filter):
    # to pass messages only from the modules
    def filter(self, record):
        return record.name.startswith("#")
        # return (
        #     record.name.startswith("#")
        #     or record.levelno > logging.DEBUG
        #     or (
        #         record.module in modules_with_info_supressed
        #         and record.levelno > logging.INFO
        #     )
        # )


class VerboseModulesFilter(logging.Filter):
    def filter(self, record):
        return True

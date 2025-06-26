DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ORIGIN_ALLOW_ALL = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "formatters": {
        "simple": {"format": "|%(levelname)s|\t|%(asctime)s|\t|%(module)s|\t'%(message)s'"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

print("Using DEV settings")

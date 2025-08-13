from django.urls import path, include


urlpatterns = [
    path("assets/", include("api.assets.urls")),
    path("devices/", include("api.devices.urls")),
    path("applications/", include("api.applications.urls")),
    path("datastreams/", include("api.datastreams.urls")),
    path("datafeeds/", include("api.datafeeds.urls")),
    path("dfreadings/", include("api.dfreadings.urls")),
    path("dsreadings/", include("api.dsreadings.urls")),
    path("nodes/", include("api.nodes.urls")),
    path("health/", include("api.health_check.urls")),
]

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    path("healthz", lambda request: HttpResponse("ok")),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("crm.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)  # attachment files (development only)

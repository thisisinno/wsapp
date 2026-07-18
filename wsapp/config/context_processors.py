from django.conf import settings


def static_asset_version(request):
    return {"STATIC_ASSET_VERSION": settings.STATIC_ASSET_VERSION}

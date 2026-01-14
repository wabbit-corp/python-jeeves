from django.urls import include, path

from ..views import ConvertView, PredictView, TrainingView, ValidateView

urlpatterns = [
    path("predict", PredictView.as_view(), name="predict"),
    path("train", TrainingView.as_view(), name="train"),
    path("validate", ValidateView.as_view(), name="validate"),
    path("convert", ConvertView.as_view(), name="convert"),
    path("statistics/", include("api.urls.statistics")),
]

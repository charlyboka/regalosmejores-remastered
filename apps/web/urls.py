from django.urls import path

from . import views

app_name = "web"

# Hub routes are declared before `<slug>` so "ocasiones" can never be swallowed as a topic slug.
urlpatterns = [
    path("", views.home, name="home"),
    path("regalos/ocasiones/", views.hub, {"hub_slug": "ocasiones"}, name="hub_ocasiones"),
    path("regalos/para-quien/", views.hub, {"hub_slug": "para-quien"}, name="hub_para_quien"),
    path("regalos/aficiones/", views.hub, {"hub_slug": "aficiones"}, name="hub_aficiones"),
    path("regalos/presupuesto/", views.hub, {"hub_slug": "presupuesto"}, name="hub_presupuesto"),
    path("regalos/<slug:slug>/", views.topic_detail, name="topic"),
    path("regalos/<slug:slug>/mas/", views.topic_more, name="topic_more"),
    path("buscar/", views.buscar, name="search"),
    path("buscador-avanzado/", views.advanced_search, name="advanced_search"),
    path("buscador-avanzado/enviar/", views.advanced_search_submit, name="advanced_submit"),
    path("buscador-avanzado/resultados/", views.advanced_results, name="advanced_results"),
    path("go/<int:product_id>/", views.go, name="go"),
    path("aviso-legal/", views.legal, {"page": "aviso-legal"}, name="aviso_legal"),
    path("privacidad/", views.legal, {"page": "privacidad"}, name="privacidad"),
    path("cookies/", views.legal, {"page": "cookies"}, name="cookies"),
    path("afiliados/", views.legal, {"page": "afiliados"}, name="afiliados"),
    path("contacto/", views.legal, {"page": "contacto"}, name="contacto"),
]

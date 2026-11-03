"""Estilos SLD básicos (um por família de geometria)."""
from __future__ import annotations

from xml.sax.saxutils import escape

CABECALHO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<StyledLayerDescriptor version="1.0.0" '
    'xsi:schemaLocation="http://www.opengis.net/sld StyledLayerDescriptor.xsd" '
    'xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
)

PADROES = {
    "poligono": {"preenchimento": "#7FB2E5", "opacidade": 0.45, "linha": "#2C5F8A", "largura": 1.0},
    "linha": {"linha": "#555555", "largura": 1.0},
    "ponto": {"preenchimento": "#E67E22", "opacidade": 0.9, "linha": "#FFFFFF", "tamanho": 6},
}


def _css(nome: str, valor) -> str:
    return f'<CssParameter name="{nome}">{escape(str(valor))}</CssParameter>'


def gerar_sld(nome: str, titulo: str, familia: str, estilo: dict | None = None) -> str:
    p = {**PADROES[familia], **(estilo or {})}
    if familia == "poligono":
        simb = (
            "<PolygonSymbolizer><Fill>" + _css("fill", p["preenchimento"]) + _css("fill-opacity", p["opacidade"])
            + "</Fill><Stroke>" + _css("stroke", p["linha"]) + _css("stroke-width", p["largura"])
            + "</Stroke></PolygonSymbolizer>"
        )
    elif familia == "linha":
        simb = (
            "<LineSymbolizer><Stroke>" + _css("stroke", p["linha"]) + _css("stroke-width", p["largura"])
            + "</Stroke></LineSymbolizer>"
        )
    else:
        simb = (
            "<PointSymbolizer><Graphic><Mark><WellKnownName>circle</WellKnownName><Fill>"
            + _css("fill", p["preenchimento"]) + _css("fill-opacity", p["opacidade"])
            + "</Fill><Stroke>" + _css("stroke", p["linha"]) + _css("stroke-width", 0.8)
            + f"</Stroke></Mark><Size>{escape(str(p['tamanho']))}</Size></Graphic></PointSymbolizer>"
        )
    return (
        CABECALHO
        + f"<NamedLayer><Name>{escape(nome)}</Name><UserStyle><Title>{escape(titulo)}</Title>"
        + f"<FeatureTypeStyle><Rule><Title>{escape(titulo)}</Title>{simb}</Rule></FeatureTypeStyle>"
        + "</UserStyle></NamedLayer></StyledLayerDescriptor>\n"
    )

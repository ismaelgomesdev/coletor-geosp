"""Tratamento dos dados: CRS, geometrias, recorte, duplicidades e conferência."""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon

log = logging.getLogger(__name__)

FAMILIA_DE_TIPO = {
    "Point": "ponto", "MultiPoint": "ponto",
    "LineString": "linha", "MultiLineString": "linha",
    "Polygon": "poligono", "MultiPolygon": "poligono",
}
CONSTRUTOR_MULTI = {"ponto": MultiPoint, "linha": MultiLineString, "poligono": MultiPolygon}
RESERVADAS = {"geom", "gid"}


class ErroGeo(RuntimeError):
    pass


def snake(nome: str) -> str:
    s = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    if not s:
        s = "col"
    if s[0].isdigit():
        s = f"c_{s}"
    return s[:55]


def padronizar_colunas(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict[str, str]]:
    """Colunas em snake_case sem acento e geometria sempre chamada 'geom'."""
    geom_atual = gdf.geometry.name
    mapa: dict[str, str] = {}
    usados = set(RESERVADAS)
    for c in gdf.columns:
        if c == geom_atual:
            continue
        novo = snake(c)
        if novo in RESERVADAS:
            novo = f"{novo}_orig"
        base, i = novo, 2
        while novo in usados:
            novo = f"{base}_{i}"
            i += 1
        usados.add(novo)
        mapa[c] = novo
    gdf = gdf.rename(columns=mapa).rename_geometry("geom")
    return gdf, mapa


def padronizar_crs(
    gdf: gpd.GeoDataFrame, crs_esperado: int | None, crs_final: int
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    info: dict[str, Any] = {"crs_arquivo": None, "crs_final": crs_final}
    if gdf.crs is None:
        if crs_esperado is None:
            raise ErroGeo("arquivo sem CRS e nenhum crs_original configurado")
        log.warning("arquivo sem CRS; assumindo EPSG:%s da configuração", crs_esperado)
        gdf = gdf.set_crs(crs_esperado)
        info["crs_arquivo"] = f"EPSG:{crs_esperado} (assumido)"
    else:
        epsg = gdf.crs.to_epsg()
        info["crs_arquivo"] = f"EPSG:{epsg}" if epsg else gdf.crs.to_string()
        if crs_esperado is not None and epsg != crs_esperado:
            log.warning("CRS do arquivo (%s) difere do configurado (EPSG:%s); usando o do arquivo",
                        info["crs_arquivo"], crs_esperado)
    return gdf.to_crs(crs_final), info


def _partes(g):
    if g.geom_type == "GeometryCollection":
        return [p for sub in g.geoms for p in _partes(sub)]
    if g.geom_type.startswith("Multi"):
        return list(g.geoms)
    return [g]


def normalizar_multi(g, familia: str):
    """Fica só com as partes do tipo esperado e devolve Multi*, ou None se não sobrar nada."""
    if g is None or g.is_empty:
        return None
    partes = [p for p in _partes(g) if FAMILIA_DE_TIPO.get(p.geom_type) == familia and not p.is_empty]
    if not partes:
        return None
    return CONSTRUTOR_MULTI[familia](partes)


def reparar_geometrias(gdf: gpd.GeoDataFrame, familia: str) -> tuple[gpd.GeoDataFrame, dict[str, int]]:
    """Tira nulas e vazias, repara inválidas, converte para 2D e Multi*."""
    n0 = len(gdf)
    arr = gdf.geometry.values
    gdf = gdf[~(shapely.is_missing(arr) | shapely.is_empty(arr))].copy()
    nulas = n0 - len(gdf)

    geoms = shapely.force_2d(gdf.geometry.values)
    invalidas = ~shapely.is_valid(geoms)
    n_invalidas = int(invalidas.sum())
    if n_invalidas:
        geoms = geoms.copy()
        geoms[invalidas] = shapely.make_valid(geoms[invalidas])

    novas = [normalizar_multi(g, familia) for g in geoms]
    gdf = gdf.set_geometry(gpd.GeoSeries(novas, index=gdf.index, crs=gdf.crs, name=gdf.geometry.name))
    antes = len(gdf)
    gdf = gdf[gdf.geometry.notna()].copy()
    descartadas = antes - len(gdf)
    return gdf, {
        "nulas_ou_vazias": nulas,
        "invalidas_reparadas": n_invalidas,
        "descartadas_apos_reparo": descartadas,
    }


def uniao_limite(limite: gpd.GeoDataFrame):
    u = limite.geometry.union_all()
    shapely.prepare(u)
    return u


def recortar_ou_filtrar(
    gdf: gpd.GeoDataFrame, limite_union, familia: str, *, modo: str, tolerancia_m: float
) -> tuple[gpd.GeoDataFrame, dict[str, int]]:
    """
    recortar: para fonte de cobertura ampla. Descarta o que está fora e corta linhas e
        polígonos pelo limite.
    filtrar: para fonte que já é municipal. Não mexe na geometria, só descarta o que
        está fora do limite mais a tolerância (os limites do IBGE e da Prefeitura não batem).
    """
    entrada = len(gdf)
    geom = gdf.geometry.name
    if modo == "filtrar":
        zona = shapely.buffer(limite_union, tolerancia_m)
        shapely.prepare(zona)
        manter = shapely.intersects(gdf.geometry.values, zona)
        saida = gdf[manter].copy()
        return saida, {"entrada": entrada, "saida": len(saida),
                       "removidas_fora": entrada - len(saida), "geometrias_alteradas": 0}
    if modo != "recortar":
        raise ErroGeo(f"modo de recorte desconhecido: {modo}")

    geoms = gdf.geometry.values
    dentro = shapely.intersects(geoms, limite_union)
    gdf = gdf[dentro].copy()
    alteradas = 0
    if familia != "ponto" and len(gdf):
        originais = gdf.geometry.values
        cortadas = shapely.intersection(originais, limite_union)
        novas = [normalizar_multi(g, familia) for g in cortadas]
        alteradas = int((~shapely.equals(originais, [g if g is not None else shapely.Point() for g in novas])).sum())
        gdf = gdf.set_geometry(gpd.GeoSeries(novas, index=gdf.index, crs=gdf.crs, name=geom))
        gdf = gdf[gdf.geometry.notna()].copy()
    return gdf, {"entrada": entrada, "saida": len(gdf),
                 "removidas_fora": entrada - len(gdf), "geometrias_alteradas": alteradas}


def verificar_limite(gdf: gpd.GeoDataFrame, limite_union, tolerancia_m: float) -> dict[str, Any]:
    """Conta as feições que ficaram fora do limite (considerando a tolerância)."""
    if not len(gdf):
        return {"total": 0, "fora": 0, "max_distancia_m": 0.0}
    zona = shapely.buffer(limite_union, tolerancia_m)
    shapely.prepare(zona)
    dentro = shapely.covers(zona, gdf.geometry.values)
    fora = ~dentro
    dist = shapely.distance(gdf.geometry.values[fora], limite_union) if fora.any() else []
    return {
        "total": int(len(gdf)),
        "fora": int(fora.sum()),
        "max_distancia_m": round(float(max(dist)), 2) if len(dist) else 0.0,
    }


def remover_duplicidades(
    gdf: gpd.GeoDataFrame, chave: list[str] | None = None
) -> tuple[gpd.GeoDataFrame, int]:
    n0 = len(gdf)
    if chave:
        faltando = [c for c in chave if c not in gdf.columns]
        if faltando:
            raise ErroGeo(f"colunas de chave inexistentes: {faltando}")
        gdf = gdf.drop_duplicates(subset=chave, keep="first")
    else:
        attrs = pd.DataFrame(gdf.drop(columns=gdf.geometry.name)).astype(str)
        attrs["__g"] = shapely.to_wkb(gdf.geometry.values)
        gdf = gdf[~attrs.duplicated(keep="first").values]
    return gdf.copy(), n0 - len(gdf)


def dicionario_atributos(gdf: gpd.GeoDataFrame) -> list[dict[str, Any]]:
    itens = []
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        s = gdf[c]
        nao_nulos = s.dropna()
        exemplo = str(nao_nulos.iloc[0])[:40] if len(nao_nulos) else None
        itens.append({"nome": c, "tipo": str(s.dtype), "nulos": int(s.isna().sum()), "exemplo": exemplo})
    return itens


def area_km2(gdf: gpd.GeoDataFrame) -> float:
    return float(gdf.geometry.area.sum() / 1e6)

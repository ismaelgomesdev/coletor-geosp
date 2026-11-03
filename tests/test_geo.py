import geopandas as gpd
import pytest
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from coletor import geo
from tests.conftest import CRS, X0, X1, Y0, Y1


def test_snake_e_colisoes():
    gdf = gpd.GeoDataFrame(
        {"Nome Distrito": [1], "NOME_DISTRITO": [2], "área (km²)": [3], "1a": [4], "GEOM": [5], "gid": [6]},
        geometry=[Point(0, 0)], crs=CRS)
    novo, mapa = geo.padronizar_colunas(gdf)
    cols = list(novo.columns)
    assert novo.geometry.name == "geom"
    assert len(cols) == len(set(cols))
    assert mapa["Nome Distrito"] == "nome_distrito"
    assert mapa["NOME_DISTRITO"] == "nome_distrito_2"
    assert mapa["área (km²)"] == "area_km2"
    assert mapa["1a"] == "c_1a"
    assert mapa["GEOM"] == "geom_orig" and mapa["gid"] == "gid_orig"


def test_reparar_geometrias():
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])  # inválido (gravata)
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2, 3, 4]},
        geometry=[bowtie, box(20, 20, 30, 30), Polygon(), None], crs=CRS)
    out, rep = geo.reparar_geometrias(gdf, "poligono")
    assert rep["nulas_ou_vazias"] == 2
    assert rep["invalidas_reparadas"] == 1
    assert len(out) == 2
    assert out.geometry.is_valid.all()
    assert set(out.geom_type) == {"MultiPolygon"}


def test_reparar_forca_2d_e_descarta_familia_errada():
    p3d = shapely.from_wkt("POINT Z (1 2 3)")
    gdf = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[p3d, LineString([(0, 0), (1, 1)])], crs=CRS)
    out, _ = geo.reparar_geometrias(gdf, "ponto")
    assert len(out) == 1  # a linha não é da família ponto
    assert not out.geometry.iloc[0].has_z
    assert out.geometry.iloc[0].geom_type == "MultiPoint"


def test_recortar_poligonos_e_pontos(limite_gdf):
    u = geo.uniao_limite(limite_gdf)
    cruza = box(X1 - 5000, Y0 + 1000, X1 + 5000, Y0 + 6000)  # metade dentro
    dentro = box(X0 + 100, Y0 + 100, X0 + 900, Y0 + 900)
    fora = box(X1 + 10000, Y1 + 10000, X1 + 11000, Y1 + 11000)
    gdf = gpd.GeoDataFrame({"id": [1, 2, 3]}, geometry=[cruza, dentro, fora], crs=CRS)
    gdf, _ = geo.reparar_geometrias(gdf, "poligono")
    out, st = geo.recortar_ou_filtrar(gdf, u, "poligono", modo="recortar", tolerancia_m=100)
    assert st == {"entrada": 3, "saida": 2, "removidas_fora": 1, "geometrias_alteradas": 1}
    assert out.geometry.area.max() == pytest.approx(5000 * 5000)  # só a metade interna sobra
    assert geo.verificar_limite(out, u, 0.01)["fora"] == 0

    pts = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(X0 + 1, Y0 + 1), Point(X1 + 500, Y1 + 500)], crs=CRS)
    pts, _ = geo.reparar_geometrias(pts, "ponto")
    out, st = geo.recortar_ou_filtrar(pts, u, "ponto", modo="recortar", tolerancia_m=100)
    assert len(out) == 1 and st["removidas_fora"] == 1


def test_recortar_linha_que_cruza(limite_gdf):
    u = geo.uniao_limite(limite_gdf)
    linha = LineString([(X0 - 5000, Y0 + 5000), (X0 + 5000, Y0 + 5000)])
    gdf, _ = geo.reparar_geometrias(gpd.GeoDataFrame({"id": [1]}, geometry=[linha], crs=CRS), "linha")
    out, st = geo.recortar_ou_filtrar(gdf, u, "linha", modo="recortar", tolerancia_m=100)
    assert out.geometry.iloc[0].length == pytest.approx(5000)
    assert st["geometrias_alteradas"] == 1


def test_filtrar_respeita_tolerancia_e_nao_altera_geometria(limite_gdf):
    u = geo.uniao_limite(limite_gdf)
    perto = Point(X1 + 50, Y0 + 10)     # 50 m fora: dentro da tolerância de 100 m
    longe = Point(X1 + 5000, Y0 + 10)   # claramente de outro município
    gdf, _ = geo.reparar_geometrias(gpd.GeoDataFrame({"id": [1, 2]}, geometry=[perto, longe], crs=CRS), "ponto")
    out, st = geo.recortar_ou_filtrar(gdf, u, "ponto", modo="filtrar", tolerancia_m=100)
    assert list(out.id) == [1]
    assert st["geometrias_alteradas"] == 0
    # QC com tolerância zero acusa o ponto de 50 m fora
    qc = geo.verificar_limite(out, u, 0)
    assert qc["fora"] == 1 and qc["max_distancia_m"] == pytest.approx(50, abs=0.1)


def test_modo_desconhecido(limite_gdf):
    with pytest.raises(geo.ErroGeo):
        geo.recortar_ou_filtrar(limite_gdf, geo.uniao_limite(limite_gdf), "poligono", modo="x", tolerancia_m=1)


def test_duplicidades():
    g = box(0, 0, 1, 1)
    gdf = gpd.GeoDataFrame({"a": ["x", "x", "y"], "k": [1, 1, 2]}, geometry=[g, g, g], crs=CRS)
    out, n = geo.remover_duplicidades(gdf)
    assert n == 1 and len(out) == 2
    out, n = geo.remover_duplicidades(gdf, ["k"])
    assert n == 1
    with pytest.raises(geo.ErroGeo):
        geo.remover_duplicidades(gdf, ["nao_existe"])


def test_crs():
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-46.63, -23.55)], crs=4674)
    out, info = geo.padronizar_crs(gdf, 4674, CRS)
    assert out.crs.to_epsg() == CRS
    x, y = out.geometry.iloc[0].x, out.geometry.iloc[0].y
    assert 320000 < x < 345000 and 7390000 < y < 7400000  # centro de São Paulo em UTM 23S
    assert info["crs_arquivo"] == "EPSG:4674"
    sem = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(-46.63, -23.55)])
    with pytest.raises(geo.ErroGeo):
        geo.padronizar_crs(sem, None, CRS)
    out, info = geo.padronizar_crs(sem, 4674, CRS)
    assert "assumido" in info["crs_arquivo"]


def test_dicionario_atributos():
    gdf = gpd.GeoDataFrame({"nome": ["a", None], "n": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)], crs=CRS)
    d = {i["nome"]: i for i in geo.dicionario_atributos(gdf)}
    assert d["nome"]["nulos"] == 1 and d["nome"]["exemplo"] == "a"
    assert d["n"]["tipo"].startswith("int")

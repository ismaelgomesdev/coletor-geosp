"""Pipeline com dados sintéticos (arquivo_local). Com TEST_PG_* usa um PostGIS de verdade."""
import hashlib
import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box
from sqlalchemy import text

from coletor import postgis
from coletor.config import Config
from coletor.geoserver import GeoServerClient
from coletor.pipeline import executar

PROJETO = {"municipio": "São Paulo", "uf": "SP", "codigo_ibge": "3550308", "crs_final": 31983,
           "schema_pg": "sp_teste", "workspace_geoserver": "sp_municipio", "datastore_geoserver": "postgis_sp",
           "tolerancia_borda_m": 100, "wfs_srs_transporte": 4326}
FONTES = {"ibge": {"orgao": "IBGE", "portal": "https://ibge", "licenca": "L", "atribuicao": "A"},
          "geosampa": {"orgao": "PMSP", "portal": "https://gs", "licenca": "L", "atribuicao": "A"}}


@pytest.fixture
def mundo(tmp_path):
    """Três municípios (EPSG:4674), pontos e linhas de teste e as pastas temporárias."""
    muni = gpd.GeoDataFrame(
        {"CD_MUN": ["3550308", "3509502", "3548708"], "NM_MUN": ["São Paulo", "Vizinho", "Longe"]},
        geometry=[box(-46.8, -23.9, -46.4, -23.4), box(-46.4, -23.9, -46.0, -23.4), box(-50, -25, -49, -24)],
        crs=4674)
    muni.to_file(tmp_path / "municipios.gpkg", driver="GPKG")

    dentro = Point(-46.6, -23.6)
    pontos = gpd.GeoDataFrame({"Nome da Unidade": ["A", "B", "C"]},
                              geometry=[dentro, Point(-46.2, -23.6), Point(-46.6, -23.6)], crs=4674)
    pontos.loc[2, "Nome da Unidade"] = "A"  # C fica igual a A
    pontos.to_file(tmp_path / "pontos.gpkg", driver="GPKG")

    linhas = gpd.GeoDataFrame({"via": ["cruza", "dentro"]},
                              geometry=[LineString([(-46.9, -23.6), (-46.5, -23.6)]),
                                        LineString([(-46.7, -23.5), (-46.6, -23.5)])], crs=4674)
    linhas.to_file(tmp_path / "linhas.gpkg", driver="GPKG")

    env = {"dir_logs": str(tmp_path / "logs"), "dir_catalogo": str(tmp_path / "catalogo"), "dir_dados": str(tmp_path / "dados"),
           "gs_pg_host": "db", "gs_pg_port": "5432", "gs_pg_user": "ro", "gs_pg_password": "x", "pg_db": "geosp"}
    camadas = [
        {"id": "limite_municipal", "ativa": True, "papel": "limite", "fonte": "ibge", "titulo": "Limite", "geometria": "poligono",
         "cobertura_fonte": "ampla", "tipo_acesso": "arquivo_local", "caminho": str(tmp_path / "municipios.gpkg"),
         "filtro_atributo": {"atributo": "cd_mun", "valor": "3550308"}, "crs_original": 4674,
         "min_registros": 1, "max_registros": 1, "verificacao": "teste"},
        {"id": "pontos_amplos", "ativa": True, "fonte": "geosampa", "titulo": "Pontos", "geometria": "ponto",
         "cobertura_fonte": "ampla", "tipo_acesso": "arquivo_local", "caminho": str(tmp_path / "pontos.gpkg"),
         "crs_original": 4674, "min_registros": 1, "verificacao": "teste"},
        {"id": "linhas_amplas", "ativa": True, "fonte": "geosampa", "titulo": "Linhas", "geometria": "linha",
         "cobertura_fonte": "ampla", "tipo_acesso": "arquivo_local", "caminho": str(tmp_path / "linhas.gpkg"),
         "crs_original": 4674, "min_registros": 2, "verificacao": "teste"},
    ]
    cfg = Config({"projeto": PROJETO, "fontes": FONTES, "camadas": camadas})
    return cfg, env, tmp_path


def rodar(cfg, env, tmp, id_, **kw):
    return executar(cfg, id_, env=env, dir_dados=tmp / "dados", **kw)


def test_limite_exige_ser_executado_primeiro(mundo):
    cfg, env, tmp = mundo
    r = rodar(cfg, env, tmp, "pontos_amplos")
    assert r.status == "falha" and r.etapa_falha == "recorte" and "execute primeiro" in r.erro


def test_fluxo_sem_banco(mundo):
    cfg, env, tmp = mundo
    r = rodar(cfg, env, tmp, "limite_municipal")
    assert r.status == "ok", r.erro
    assert r.estatisticas["registros_lidos"] == 3 and r.estatisticas["registros_final"] == 1

    r = rodar(cfg, env, tmp, "pontos_amplos")
    assert r.status == "ok", r.erro
    e = r.estatisticas
    assert e["recorte"]["removidas_fora"] == 1  # ponto B, do município vizinho
    assert e["duplicidades_removidas"] == 1  # C repete A
    assert e["registros_final"] == 1
    assert e["qc_fora_do_limite"]["fora"] == 0

    r = rodar(cfg, env, tmp, "linhas_amplas")
    assert r.status == "ok", r.erro
    assert r.estatisticas["recorte"]["geometrias_alteradas"] == 1  # a linha que cruza o limite foi cortada
    saida = gpd.read_file(tmp / "dados" / "processado" / "linhas_amplas.gpkg")
    lim = gpd.read_file(tmp / "dados" / "processado" / "limite_municipal.gpkg")
    assert saida.crs.to_epsg() == 31983 and len(saida) == 2
    assert lim.geometry.union_all().buffer(0.01).covers(saida.geometry.union_all())

    cat = json.loads((tmp / "catalogo" / "pontos_amplos.json").read_text(encoding="utf-8"))
    assert cat["crs_final"] == "EPSG:31983" and cat["quantidade_registros"] == 1
    assert cat["dicionario_atributos"][0]["nome"] == "nome_da_unidade"
    assert any("Recorte" in x for x in cat["regras_transformacao"])
    ops = [json.loads(linha) for linha in (tmp / "logs" / "operacoes.jsonl").read_text().splitlines()]
    assert [o["status"] for o in ops].count("ok") == 3


def test_falha_de_qc_preserva_versao_anterior(mundo):
    cfg, env, tmp = mundo
    assert rodar(cfg, env, tmp, "limite_municipal").status == "ok"
    assert rodar(cfg, env, tmp, "linhas_amplas").status == "ok"
    arq = tmp / "dados" / "processado" / "linhas_amplas.gpkg"
    antes = hashlib.sha256(arq.read_bytes()).hexdigest()

    cfg.camada("linhas_amplas")["min_registros"] = 50   # QC passa a reprovar
    r = rodar(cfg, env, tmp, "linhas_amplas")
    assert r.status == "falha" and r.etapa_falha == "qc"
    assert hashlib.sha256(arq.read_bytes()).hexdigest() == antes


def test_filtro_inexistente_e_area_fora_do_esperado(mundo):
    cfg, env, tmp = mundo
    cfg.camada("limite_municipal")["filtro_atributo"] = {"atributo": "CD_MUN", "valor": "0000000"}
    r = rodar(cfg, env, tmp, "limite_municipal")
    assert r.status == "falha" and r.etapa_falha == "filtro"
    cfg.camada("limite_municipal")["filtro_atributo"] = {"atributo": "CD_MUN", "valor": "3550308"}
    cfg.camada("limite_municipal")["area_km2_esperada"] = [10, 20]
    r = rodar(cfg, env, tmp, "limite_municipal")
    assert r.status == "falha" and "fora do intervalo" in r.erro


def test_carga_postgis_troca_atomica_e_geoserver_falso(mundo, engine_pg, geoserver_falso):
    cfg, env, tmp = mundo
    gs = GeoServerClient(geoserver_falso.url, "admin", "segredo")
    for id_ in ("limite_municipal", "pontos_amplos"):
        r = rodar(cfg, env, tmp, id_, engine=engine_pg, gs=gs)
        assert r.status == "ok", r.erro
    assert r.servicos["wms"] and r.servicos["wfs"]

    with engine_pg.connect() as con:
        assert con.execute(text("SELECT count(*) FROM sp_teste.pontos_amplos")).scalar() == 1
        assert con.execute(text("SELECT Find_SRID('sp_teste','pontos_amplos','geom')")).scalar() == 31983
        assert con.execute(text("SELECT count(*) FROM sp_teste.pontos_amplos WHERE NOT ST_Within(geom, "
                                "(SELECT geom FROM sp_teste.limite_municipal LIMIT 1))")).scalar() == 0
        assert con.execute(text("SELECT count(*) FROM pg_indexes WHERE schemaname='sp_teste' AND tablename='pontos_amplos' "
                                "AND indexdef ILIKE '%gist%'")).scalar() == 1
        assert con.execute(text("SELECT count(*) FROM meta.coletas WHERE camada='pontos_amplos' AND status='ok'")).scalar() == 1

    # segunda execução: fica a versão anterior e nenhuma tabela temporária
    assert rodar(cfg, env, tmp, "pontos_amplos", engine=engine_pg, gs=gs).status == "ok"
    with engine_pg.connect() as con:
        tabelas = {r[0] for r in con.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='sp_teste'"))}
    assert {"pontos_amplos", "pontos_amplos__anterior", "limite_municipal"} <= tabelas
    assert not [t for t in tabelas if "__n" in t]
    # o workspace só pode ter sido criado uma vez
    assert [r for r in geoserver_falso.requisicoes if r == ("POST", "/workspaces")] == [("POST", "/workspaces")]


def test_falha_na_carga_nao_toca_na_tabela_atual(mundo, engine_pg):
    cfg, env, tmp = mundo
    assert rodar(cfg, env, tmp, "limite_municipal", engine=engine_pg).status == "ok"
    gdf = gpd.read_file(tmp / "dados" / "processado" / "limite_municipal.gpkg")
    with pytest.raises(postgis.ErroCarga, match="SRID"):
        postgis.carregar_camada(engine_pg, gdf, "sp_teste", "limite_municipal", srid=4326)  # SRID errado de propósito
    with engine_pg.connect() as con:
        assert con.execute(text("SELECT count(*) FROM sp_teste.limite_municipal")).scalar() == 1
        tabelas = {r[0] for r in con.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='sp_teste'"))}
    assert not [t for t in tabelas if "__n" in t]


def test_conceder_leitura(engine_pg):
    with engine_pg.begin() as con:
        con.execute(text("DROP ROLE IF EXISTS leitor_teste"))
    assert postgis.conceder_leitura(engine_pg, "sp_teste", "leitor_teste") is False
    with engine_pg.begin() as con:
        con.execute(text("CREATE ROLE leitor_teste"))
    try:
        assert postgis.conceder_leitura(engine_pg, "sp_teste", "leitor_teste") is True
        with engine_pg.connect() as con:
            assert con.execute(text("SELECT has_schema_privilege('leitor_teste', 'sp_teste', 'USAGE')")).scalar()
    finally:
        with engine_pg.begin() as con:
            con.execute(text("DROP SCHEMA IF EXISTS sp_teste CASCADE"))
            con.execute(text("DROP ROLE IF EXISTS leitor_teste"))


def test_catalogo_traz_periodicidade_e_metadados(mundo):
    cfg, env, tmp = mundo
    cfg.camada("limite_municipal")["periodicidade"] = "anual"
    cfg.camada("limite_municipal")["url_metadados"] = "https://exemplo/meta"
    assert rodar(cfg, env, tmp, "limite_municipal").status == "ok"
    cat = json.loads((tmp / "catalogo" / "limite_municipal.json").read_text(encoding="utf-8"))
    assert cat["periodicidade"] == "anual" and cat["url_metadados"] == "https://exemplo/meta"
    assert cat["sha256_fonte"] and len(cat["sha256_fonte"]) == 64

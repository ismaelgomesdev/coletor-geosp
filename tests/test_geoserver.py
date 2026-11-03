import pytest

from coletor.estilos import gerar_sld
from coletor.geoserver import ErroGeoServer, GeoServerClient

DS = dict(host="db", porta="5432", banco="geosp", usuario="ro", senha="x", schema="sp")


def cliente(estado, senha="segredo"):
    return GeoServerClient(estado.url, "admin", senha)


def test_versao_e_credenciais_erradas(geoserver_falso):
    assert cliente(geoserver_falso).versao() == "2.99.0"
    with pytest.raises(ErroGeoServer, match="credenciais") as exc:
        cliente(geoserver_falso, senha="errada").versao()
    assert "errada" not in str(exc.value)


def test_cadastro_idempotente(geoserver_falso):
    gs = cliente(geoserver_falso)
    assert gs.garantir_workspace("sp_municipio") is True
    assert gs.garantir_workspace("sp_municipio") is False
    assert gs.garantir_datastore("sp_municipio", "postgis_sp", **DS) is True
    assert gs.garantir_datastore("sp_municipio", "postgis_sp", **DS) is False

    sld = gerar_sld("ubs_estilo", "UBS", "ponto")
    gs.garantir_estilo("sp_municipio", "ubs_estilo", sld)
    gs.garantir_estilo("sp_municipio", "ubs_estilo", sld)

    kw = dict(nome="ubs", tabela="ubs", titulo="UBS", descricao="d", srs=31983, estilo="ubs_estilo")
    assert gs.publicar_camada("sp_municipio", "postgis_sp", **kw) == "criada"
    assert gs.publicar_camada("sp_municipio", "postgis_sp", **kw) == "atualizada"

    posts = [r for r in geoserver_falso.requisicoes if r[0] == "POST"]
    # exatamente um POST por recurso, mesmo após duas execuções
    assert posts == [("POST", "/workspaces"), ("POST", "/workspaces/sp_municipio/datastores"),
                     ("POST", "/workspaces/sp_municipio/styles"),
                     ("POST", "/workspaces/sp_municipio/datastores/postgis_sp/featuretypes")]


def test_verificar_servicos(geoserver_falso):
    r = cliente(geoserver_falso).verificar_servicos("sp_municipio", "ubs", (0, 0, 10, 10), 31983)
    assert r["wms"] is True and r["wfs"] is True


def test_geoserver_inacessivel():
    gs = GeoServerClient("http://127.0.0.1:9/geoserver", "a", "b", timeout=2)
    with pytest.raises(ErroGeoServer, match="sem conexão"):
        gs.versao()


def test_sld_por_familia():
    for fam, tag in (("poligono", "PolygonSymbolizer"), ("linha", "LineSymbolizer"), ("ponto", "PointSymbolizer")):
        sld = gerar_sld("n", "Título <&>", fam)
        assert tag in sld and "Título &lt;&amp;&gt;" in sld

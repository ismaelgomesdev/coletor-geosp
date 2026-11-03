import json
import zipfile

import pytest
import requests

from coletor import fetch


class RespostaFalsa:
    def __init__(self, status=200, conteudo=b"", headers=None, url="http://x"):
        self.status_code, self.content, self.headers, self.url = status, conteudo, headers or {}, url

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def iter_content(self, chunk_size=1):
        yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class SessaoFalsa:
    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def get(self, url, params=None, **kw):
        self.chamadas.append(params)
        r = self.respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


sem_espera = lambda s: None  # noqa: E731


def test_download_com_retentativa_e_hash(tmp_path):
    s = SessaoFalsa([RespostaFalsa(503), requests.ConnectionError("caiu"),
                     RespostaFalsa(200, b"dados", {"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"})])
    info = fetch.baixar_arquivo("http://x/a.zip", tmp_path / "a.zip", sessao=s, espera=sem_espera)
    assert (tmp_path / "a.zip").read_bytes() == b"dados"
    assert not (tmp_path / "a.zip.part").exists()
    assert info["bytes"] == 5 and len(info["sha256"]) == 64
    assert info["last_modified"].startswith("Mon, 01 Jan 2024")


def test_download_falha_apos_tentativas_e_resposta_vazia(tmp_path):
    s = SessaoFalsa([RespostaFalsa(503)] * 3)
    with pytest.raises(fetch.ErroFonte, match="3 tentativas"):
        fetch.baixar_arquivo("http://x", tmp_path / "a", sessao=s, tentativas=3, espera=sem_espera)
    assert not (tmp_path / "a").exists()
    with pytest.raises(fetch.ErroFonte, match="vazia"):
        fetch.baixar_arquivo("http://x", tmp_path / "b", sessao=SessaoFalsa([RespostaFalsa(200, b"")]), espera=sem_espera)


def test_erro_4xx_nao_repete(tmp_path):
    s = SessaoFalsa([RespostaFalsa(404), RespostaFalsa(200, b"x")])
    with pytest.raises(fetch.ErroPedido, match="404"):
        fetch.baixar_arquivo("http://x", tmp_path / "a", sessao=s, espera=sem_espera)
    assert len(s.respostas) == 1


def test_zip_seguro(tmp_path):
    ok = tmp_path / "ok.zip"
    with zipfile.ZipFile(ok, "w") as z:
        z.writestr("pasta/dado.shp", b"x")
    arqs = fetch.extrair_zip_seguro(ok, tmp_path / "out")
    assert [a.name for a in arqs] == ["dado.shp"]
    assert fetch.localizar_vetor(tmp_path / "out").name == "dado.shp"

    mau = tmp_path / "mau.zip"
    with zipfile.ZipFile(mau, "w") as z:
        z.writestr("../fora.txt", b"x")
    with pytest.raises(fetch.ErroFonte, match="inseguro"):
        fetch.extrair_zip_seguro(mau, tmp_path / "out2")
    assert not (tmp_path / "fora.txt").exists()


def test_localizar_vetor_ambiguo(tmp_path):
    (tmp_path / "a.shp").write_bytes(b"")
    (tmp_path / "b.shp").write_bytes(b"")
    with pytest.raises(fetch.ErroFonte, match="mais de um"):
        fetch.localizar_vetor(tmp_path)
    with pytest.raises(fetch.ErroFonte, match="nenhum"):
        fetch.localizar_vetor(tmp_path, "*.gpkg")


def _fc(n, **extra):
    return json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": None}] * n, **extra}).encode()


def test_wfs_v1_unica_requisicao(tmp_path):
    s = SessaoFalsa([RespostaFalsa(200, _fc(3, totalFeatures=3))])
    info = fetch.baixar_wfs("http://w", "geoportal:x", tmp_path / "x.geojson", sessao=s, espera=sem_espera)
    assert info["feicoes"] == 3
    p = s.chamadas[0]
    assert p["typeName"] == "geoportal:x" and p["srsName"] == "EPSG:4326" and "startIndex" not in p


def test_wfs_v2_paginado(tmp_path):
    s = SessaoFalsa([RespostaFalsa(200, _fc(2, numberMatched=3)), RespostaFalsa(200, _fc(1, numberMatched=3))])
    info = fetch.baixar_wfs("http://w", "geoportal:x", tmp_path / "x.geojson", versao="2.0.0", pagina=2,
                            sessao=s, espera=sem_espera)
    assert info["feicoes"] == 3
    assert [c["startIndex"] for c in s.chamadas] == [0, 2]
    assert s.chamadas[0]["typeNames"] == "geoportal:x"


def test_wfs_incompleto_e_respostas_invalidas(tmp_path):
    with pytest.raises(fetch.ErroFonte, match="incompleto"):
        fetch.baixar_wfs("http://w", "t", tmp_path / "a", sessao=SessaoFalsa([RespostaFalsa(200, _fc(3, totalFeatures=5))]))
    with pytest.raises(fetch.ErroFonte, match="não é JSON"):
        fetch.baixar_wfs("http://w", "t", tmp_path / "b",
                         sessao=SessaoFalsa([RespostaFalsa(200, b"<ows:ExceptionReport>erro</ows:ExceptionReport>")]))
    with pytest.raises(fetch.ErroFonte, match="nenhuma feição"):
        fetch.baixar_wfs("http://w", "t", tmp_path / "c", sessao=SessaoFalsa([RespostaFalsa(200, _fc(0))]))
    assert not (tmp_path / "a").exists()


CAPS = b"""<?xml version="1.0"?>
<WFS_Capabilities version="1.0.0" xmlns="http://www.opengis.net/wfs">
 <FeatureTypeList>
  <FeatureType><Name>geoportal:equipamento_saude_ubs_posto_centro</Name><Title>UBS</Title></FeatureType>
  <FeatureType><Name>geoportal:distrito_municipal</Name><Title>Distritos</Title></FeatureType>
 </FeatureTypeList>
</WFS_Capabilities>"""


def test_descobrir_wfs():
    itens = fetch.descobrir_wfs("http://w", sessao=SessaoFalsa([RespostaFalsa(200, CAPS)]))
    assert [i["type_name"] for i in itens] == ["geoportal:equipamento_saude_ubs_posto_centro", "geoportal:distrito_municipal"]
    with pytest.raises(fetch.ErroFonte):
        fetch.descobrir_wfs("http://w", sessao=SessaoFalsa([RespostaFalsa(200, b"nao e xml")]))

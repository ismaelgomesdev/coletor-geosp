import copy

import pytest
import yaml

from coletor.config import CAMINHO_PADRAO, Config, ErroConfig, carregar


def test_fontes_yaml_carrega_com_as_12_camadas():
    cfg = carregar()
    assert len(cfg.camadas) == 12
    assert {c["fonte"] for c in cfg.camadas} == {"geosampa", "ibge", "sptrans"}
    assert cfg.limite()["id"] == "limite_municipal"
    assert [c["id"] for c in cfg.ativas()] == ["limite_municipal", "ubs_unidades_saude"]
    assert cfg.projeto["codigo_ibge"] == "3550308"


def _dados():
    return copy.deepcopy(yaml.safe_load(open(CAMINHO_PADRAO, encoding="utf-8")))


def test_ativar_camada_incompleta_e_recusado():
    d = _dados()
    for c in d["camadas"]:
        if c["id"] == "distritos":
            c["ativa"] = True
            c["type_name"] = None
    with pytest.raises(ErroConfig, match="type_name"):
        Config(d)


def test_id_invalido_e_duplicado_e_fonte_inexistente():
    d = _dados()
    d["camadas"][0]["id"] = "Drop Table;"
    with pytest.raises(ErroConfig, match="inválido"):
        Config(d)
    d = _dados()
    d["camadas"][1]["id"] = d["camadas"][0]["id"]
    with pytest.raises(ErroConfig, match="duplicado"):
        Config(d)
    d = _dados()
    d["camadas"][0]["fonte"] = "inexistente"
    with pytest.raises(ErroConfig, match="não cadastrada"):
        Config(d)


def test_camada_desconhecida_lista_opcoes():
    with pytest.raises(ErroConfig, match="limite_municipal"):
        carregar().camada("xyz")


def test_so_um_limite():
    d = _dados()
    for c in d["camadas"][:2]:
        c["papel"] = "limite"
    with pytest.raises(ErroConfig, match="só uma"):
        Config(d)

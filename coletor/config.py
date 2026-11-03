"""Leitura do fontes.yaml e das variáveis de ambiente."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

CAMINHO_PADRAO = Path(__file__).resolve().parent.parent / "config" / "fontes.yaml"

TIPOS_ACESSO = {"download_zip", "download_arquivo", "wfs", "arquivo_local", "gtfs"}
FAMILIAS = {"ponto", "linha", "poligono"}
COBERTURAS = {"ampla", "municipal"}
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class ErroConfig(ValueError):
    pass


class Config:
    def __init__(self, dados: dict[str, Any]):
        self.projeto: dict[str, Any] = dados.get("projeto") or {}
        self.fontes: dict[str, dict[str, Any]] = dados.get("fontes") or {}
        self.camadas: list[dict[str, Any]] = dados.get("camadas") or []
        self._validar()

    def camada(self, id_: str) -> dict[str, Any]:
        for c in self.camadas:
            if c["id"] == id_:
                return c
        raise ErroConfig(f"camada desconhecida: {id_!r}. Disponíveis: {', '.join(self.ids())}")

    def ids(self) -> list[str]:
        return [c["id"] for c in self.camadas]

    def ativas(self) -> list[dict[str, Any]]:
        return [c for c in self.camadas if c.get("ativa")]

    def limite(self) -> dict[str, Any] | None:
        for c in self.camadas:
            if c.get("papel") == "limite":
                return c
        return None

    def _validar(self) -> None:
        for chave in ("codigo_ibge", "crs_final", "schema_pg", "workspace_geoserver", "datastore_geoserver"):
            if not self.projeto.get(chave):
                raise ErroConfig(f"projeto.{chave} ausente em fontes.yaml")
        for nome in ("schema_pg", "workspace_geoserver", "datastore_geoserver"):
            if not SLUG.match(str(self.projeto[nome])):
                raise ErroConfig(f"projeto.{nome} deve ser um identificador simples (a-z, 0-9, _)")

        vistos: set[str] = set()
        for c in self.camadas:
            id_ = c.get("id", "")
            if not SLUG.match(str(id_)):
                raise ErroConfig(f"id de camada inválido: {id_!r} (use a-z, 0-9 e _, até 40 caracteres)")
            if id_ in vistos:
                raise ErroConfig(f"id de camada duplicado: {id_}")
            vistos.add(id_)
            if c.get("fonte") not in self.fontes:
                raise ErroConfig(f"{id_}: fonte {c.get('fonte')!r} não cadastrada em 'fontes'")
            if c.get("geometria") not in FAMILIAS:
                raise ErroConfig(f"{id_}: geometria deve ser uma de {sorted(FAMILIAS)}")
            if c.get("cobertura_fonte") not in COBERTURAS:
                raise ErroConfig(f"{id_}: cobertura_fonte deve ser uma de {sorted(COBERTURAS)}")
            if c.get("tipo_acesso") not in TIPOS_ACESSO:
                raise ErroConfig(f"{id_}: tipo_acesso deve ser um de {sorted(TIPOS_ACESSO)}")
            if c.get("ativa"):
                self._validar_ativa(c)

        if len([c for c in self.camadas if c.get("papel") == "limite"]) > 1:
            raise ErroConfig("só uma camada pode ter papel: limite")

    def _validar_ativa(self, c: dict[str, Any]) -> None:
        id_ = c["id"]
        acesso = c["tipo_acesso"]
        if acesso in {"download_zip", "download_arquivo"} and not c.get("url"):
            raise ErroConfig(f"{id_}: camada ativa com download precisa de 'url'")
        if acesso == "wfs" and not (c.get("wfs_url") and c.get("type_name")):
            raise ErroConfig(f"{id_}: camada ativa via WFS precisa de 'wfs_url' e 'type_name'")
        if acesso == "arquivo_local" and not c.get("caminho"):
            raise ErroConfig(f"{id_}: arquivo_local precisa de 'caminho'")
        if acesso == "gtfs":
            raise ErroConfig(f"{id_}: conector GTFS ainda não implementado")


def carregar(caminho: Path | str | None = None) -> Config:
    caminho = Path(caminho) if caminho else CAMINHO_PADRAO
    with open(caminho, encoding="utf-8") as f:
        dados = yaml.safe_load(f)
    if not isinstance(dados, dict):
        raise ErroConfig(f"{caminho} não contém um mapeamento YAML")
    return Config(dados)


def carregar_env(caminho: Path | str = ".env") -> None:
    """Lê o .env sem sobrescrever o que já está definido no ambiente."""
    p = Path(caminho)
    if not p.is_file():
        return
    for linha in p.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.strip().strip('"').strip("'")
        os.environ.setdefault(chave.strip(), valor)


def ambiente() -> dict[str, str]:
    e = os.environ
    pg_host = e.get("PG_HOST", "localhost")
    pg_port = e.get("PG_PORT", "5432")
    pg_user = e.get("PG_USER", "coletor")
    return {
        "pg_host": pg_host,
        "pg_port": pg_port,
        "pg_db": e.get("PG_DB", "geosp"),
        "pg_user": pg_user,
        "pg_password": e.get("PG_PASSWORD", ""),
        "gs_url": e.get("GEOSERVER_URL", "http://localhost:8080/geoserver"),
        "gs_user": e.get("GEOSERVER_USER", "admin"),
        "gs_password": e.get("GEOSERVER_PASSWORD", ""),
        "gs_pg_host": e.get("GEOSERVER_PG_HOST", pg_host),
        "gs_pg_port": e.get("GEOSERVER_PG_PORT", pg_port),
        "gs_pg_user": e.get("GEOSERVER_PG_USER", pg_user),
        "gs_pg_password": e.get("GEOSERVER_PG_PASSWORD", e.get("PG_PASSWORD", "")),
        "dir_dados": e.get("DIR_DADOS", "dados"),
        "dir_logs": e.get("DIR_LOGS", "logs"),
        "dir_catalogo": e.get("DIR_CATALOGO", "catalogo"),
    }

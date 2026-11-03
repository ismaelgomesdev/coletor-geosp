"""Cliente da API REST do GeoServer (workspace, datastore, estilo e camada) e teste de WMS/WFS."""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

log = logging.getLogger(__name__)


class ErroGeoServer(RuntimeError):
    pass


class GeoServerClient:
    def __init__(self, url: str, usuario: str, senha: str, *, sessao: requests.Session | None = None,
                 timeout: int = 60):
        self.base = url.rstrip("/")
        self.rest = f"{self.base}/rest"
        self.s = sessao or requests.Session()
        self.s.auth = (usuario, senha)
        self.s.headers.update({"Accept": "application/json", "User-Agent": "coletor-geosp/0.1"})
        self.timeout = timeout

    def _req(self, metodo: str, caminho: str, *, ok=(200, 201), json_body=None, dados=None,
             tipo: str | None = None, params=None, permitir_404=False) -> requests.Response | None:
        url = f"{self.rest}{caminho}"
        headers = {}
        if tipo:
            headers["Content-Type"] = tipo
        elif json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            r = self.s.request(metodo, url, json=json_body, data=dados, headers=headers,
                               params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ErroGeoServer(f"{metodo} {caminho}: sem conexão com o GeoServer ({type(exc).__name__})") from exc
        if r.status_code == 404 and permitir_404:
            return None
        if r.status_code == 401:
            raise ErroGeoServer("GeoServer recusou as credenciais (HTTP 401): confira GEOSERVER_USER/PASSWORD")
        if r.status_code not in ok:
            raise ErroGeoServer(f"{metodo} {caminho}: HTTP {r.status_code}, {r.text[:300]}")
        return r

    def versao(self) -> str:
        r = self._req("GET", "/about/version.json")
        try:
            recursos = r.json()["about"]["resource"]
            for item in recursos:
                if item.get("@name") == "GeoServer":
                    return item.get("Version", "?")
        except (ValueError, KeyError, TypeError):
            pass
        return "?"

    def garantir_workspace(self, ws: str) -> bool:
        if self._req("GET", f"/workspaces/{ws}.json", permitir_404=True):
            return False
        self._req("POST", "/workspaces", json_body={"workspace": {"name": ws}}, ok=(201,))
        log.info("workspace %s criado", ws)
        return True

    def garantir_datastore(self, ws: str, ds: str, *, host: str, porta: str, banco: str,
                           usuario: str, senha: str, schema: str) -> bool:
        """Cria o datastore ou atualiza os parâmetros se ele já existe."""
        entradas = [
            ("host", host), ("port", str(porta)), ("database", banco), ("schema", schema),
            ("user", usuario), ("passwd", senha), ("dbtype", "postgis"),
            ("Expose primary keys", "true"), ("validate connections", "true"),
        ]
        corpo = {"dataStore": {
            "name": ds, "enabled": True,
            "connectionParameters": {"entry": [{"@key": k, "$": v} for k, v in entradas]},
        }}
        existe = self._req("GET", f"/workspaces/{ws}/datastores/{ds}.json", permitir_404=True)
        if existe:
            self._req("PUT", f"/workspaces/{ws}/datastores/{ds}", json_body=corpo, ok=(200,))
            return False
        self._req("POST", f"/workspaces/{ws}/datastores", json_body=corpo, ok=(201,))
        log.info("datastore %s criado", ds)
        return True

    def garantir_estilo(self, ws: str, nome: str, sld: str) -> None:
        tipo = "application/vnd.ogc.sld+xml"
        existe = self._req("GET", f"/workspaces/{ws}/styles/{nome}.json", permitir_404=True)
        if existe:
            self._req("PUT", f"/workspaces/{ws}/styles/{nome}", dados=sld.encode("utf-8"), tipo=tipo, ok=(200,))
        else:
            self._req("POST", f"/workspaces/{ws}/styles", dados=sld.encode("utf-8"), tipo=tipo,
                      params={"name": nome}, ok=(201,))

    def publicar_camada(self, ws: str, ds: str, *, nome: str, tabela: str, titulo: str,
                        descricao: str, srs: int, estilo: str | None = None,
                        palavras_chave: list[str] | None = None) -> str:
        """Cria ou atualiza o feature type e recalcula o bbox."""
        ft = {
            "name": nome, "nativeName": tabela, "title": titulo, "abstract": descricao,
            "srs": f"EPSG:{srs}", "enabled": True, "projectionPolicy": "FORCE_DECLARED",
            "keywords": {"string": palavras_chave or ["São Paulo", "dados abertos"]},
        }
        caminho = f"/workspaces/{ws}/datastores/{ds}/featuretypes"
        existe = self._req("GET", f"{caminho}/{nome}.json", permitir_404=True)
        if existe:
            self._req("PUT", f"{caminho}/{nome}", json_body={"featureType": ft}, ok=(200,),
                      params={"recalculate": "nativebbox,latlonbbox"})
            acao = "atualizada"
        else:
            self._req("POST", caminho, json_body={"featureType": ft}, ok=(201,))
            # o POST sozinho deixa o bbox vazio
            self._req("PUT", f"{caminho}/{nome}", json_body={"featureType": ft}, ok=(200,),
                      params={"recalculate": "nativebbox,latlonbbox"})
            acao = "criada"
        if estilo:
            self._req("PUT", f"/layers/{ws}:{nome}",
                      json_body={"layer": {"defaultStyle": {"name": estilo, "workspace": ws}}}, ok=(200,))
        log.info("camada %s:%s %s", ws, nome, acao)
        return acao

    def verificar_servicos(self, ws: str, nome: str, bbox: tuple[float, float, float, float],
                           srs: int) -> dict[str, Any]:
        """GetMap tem que devolver uma imagem e GetFeature pelo menos uma feição."""
        res: dict[str, Any] = {"wms": False, "wfs": False}
        ows = f"{self.base}/{ws}/ows"
        try:
            r = self.s.get(ows, params={
                "service": "WMS", "version": "1.1.1", "request": "GetMap", "layers": f"{ws}:{nome}",
                "styles": "", "bbox": ",".join(f"{v:.2f}" for v in bbox), "width": 400, "height": 400,
                "srs": f"EPSG:{srs}", "format": "image/png",
            }, timeout=self.timeout, headers={"Accept": "image/png"})
            res["wms"] = r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/png") \
                and len(r.content) > 500
            res["wms_bytes"] = len(r.content)
        except requests.RequestException as exc:
            res["wms_erro"] = type(exc).__name__
        try:
            r = self.s.get(ows, params={
                "service": "WFS", "version": "1.1.0", "request": "GetFeature", "typeName": f"{ws}:{nome}",
                "maxFeatures": 1, "outputFormat": "application/json",
            }, timeout=self.timeout, headers={"Accept": "application/json"})
            n = len(json.loads(r.content).get("features", [])) if r.status_code == 200 else 0
            res["wfs"] = n >= 1
            res["wfs_feicoes_amostra"] = n
        except (requests.RequestException, ValueError) as exc:
            res["wfs_erro"] = type(exc).__name__
        return res

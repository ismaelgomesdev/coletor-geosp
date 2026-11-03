"""Fixtures: dados sintéticos, um GeoServer falso (HTTP local) e o PostGIS de teste."""
from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

CRS = 31983
# quadrado de 20 km por 20 km em UTM 23S, na região de São Paulo
X0, Y0, X1, Y1 = 320000, 7380000, 340000, 7400000


@pytest.fixture
def limite_gdf():
    return gpd.GeoDataFrame({"cd_mun": ["3550308"]}, geometry=[box(X0, Y0, X1, Y1)], crs=CRS)


class _Estado:
    def __init__(self):
        self.recursos: dict[str, object] = {}
        self.requisicoes: list[tuple[str, str]] = []
        self.usuario, self.senha = "admin", "segredo"


def _criar_handler(estado: _Estado):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _autorizado(self) -> bool:
            h = self.headers.get("Authorization", "")
            esperado = "Basic " + base64.b64encode(f"{estado.usuario}:{estado.senha}".encode()).decode()
            return h == esperado

        def _responder(self, codigo, corpo=b"", tipo="application/json"):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def _chave(self):
            u = urlparse(self.path)
            p = u.path.removeprefix("/geoserver/rest")
            if p.endswith(".json"):
                p = p[:-5]
            return p, parse_qs(u.query)

        def _corpo(self) -> bytes:
            n = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            u = urlparse(self.path)
            if u.path.endswith("/ows"):
                q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
                estado.requisicoes.append(("GET", f"OWS {q.get('service')}"))
                if q.get("service") == "WMS":
                    return self._responder(200, b"\x89PNG" + b"0" * 2000, "image/png")
                return self._responder(200, json.dumps({"type": "FeatureCollection", "features": [{}]}).encode())
            if not self._autorizado():
                return self._responder(401, b"unauthorized", "text/plain")
            chave, _ = self._chave()
            estado.requisicoes.append(("GET", chave))
            if chave == "/about/version":
                return self._responder(200, json.dumps({"about": {"resource": [{"@name": "GeoServer", "Version": "2.99.0"}]}}).encode())
            if chave in estado.recursos:
                return self._responder(200, b"{}")
            self._responder(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._autorizado():
                return self._responder(401, b"unauthorized", "text/plain")
            chave, q = self._chave()
            estado.requisicoes.append(("POST", chave))
            corpo = self._corpo()
            if chave.endswith("/styles"):
                nome = q["name"][0]
            else:
                dados = json.loads(corpo)
                nome = next(iter(dados.values()))["name"]
            estado.recursos[f"{chave}/{nome}"] = corpo
            self._responder(201, b"")

        def do_PUT(self):
            if not self._autorizado():
                return self._responder(401, b"unauthorized", "text/plain")
            chave, q = self._chave()
            estado.requisicoes.append(("PUT", chave))
            corpo = self._corpo()
            if chave.startswith("/layers/") or chave in estado.recursos:
                estado.recursos[chave] = corpo
                return self._responder(200, b"")
            self._responder(404, b"not found", "text/plain")

    return H


@pytest.fixture
def geoserver_falso():
    estado = _Estado()
    srv = HTTPServer(("127.0.0.1", 0), _criar_handler(estado))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    estado.url = f"http://127.0.0.1:{srv.server_address[1]}/geoserver"
    yield estado
    srv.shutdown()


def _env_pg():
    if not os.environ.get("TEST_PG_HOST"):
        return None
    return {
        "pg_host": os.environ["TEST_PG_HOST"], "pg_port": os.environ.get("TEST_PG_PORT", "5432"),
        "pg_db": os.environ.get("TEST_PG_DB", "geosp"), "pg_user": os.environ.get("TEST_PG_USER", "coletor"),
        "pg_password": os.environ.get("TEST_PG_PASSWORD", ""),
    }


@pytest.fixture
def engine_pg():
    env = _env_pg()
    if env is None:
        pytest.skip("defina TEST_PG_HOST/PORT/DB/USER/PASSWORD para testar com PostGIS real")
    from sqlalchemy import text

    from coletor import postgis
    eng = postgis.criar_engine(env)
    with eng.begin() as con:
        con.execute(text("DROP SCHEMA IF EXISTS sp_teste CASCADE"))
    postgis.preparar_banco(eng, "sp_teste")
    with eng.begin() as con:
        con.execute(text("TRUNCATE meta.coletas"))
    yield eng
    with eng.begin() as con:
        con.execute(text("DROP SCHEMA IF EXISTS sp_teste CASCADE"))
    eng.dispose()

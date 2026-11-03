"""Download de arquivos, extração de ZIP e leitura de WFS."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

USER_AGENT = "coletor-geosp/0.1 (pesquisa voluntaria; Insight Lab UFC)"
LIMITE_ZIP_BYTES = 8 * 1024**3


class ErroFonte(RuntimeError):
    pass


class ErroPedido(ErroFonte):
    """4xx (menos 429): repetir o pedido não resolve."""


def nova_sessao() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _com_tentativas(
    chamada: Callable[[], requests.Response],
    *,
    tentativas: int,
    espera: Callable[[float], None],
    descricao: str,
) -> requests.Response:
    ultimo: Exception | None = None
    for n in range(1, tentativas + 1):
        try:
            r = chamada()
            if r.status_code in (429, 500, 502, 503, 504):
                raise ErroFonte(f"HTTP {r.status_code}")
            if 400 <= r.status_code < 500:
                raise ErroPedido(f"{descricao}: HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except ErroPedido:
            raise
        except (requests.RequestException, ErroFonte) as exc:
            ultimo = exc
            log.warning("%s: tentativa %d/%d falhou (%s)", descricao, n, tentativas, exc)
            if n < tentativas:
                espera(2 ** n)
    raise ErroFonte(f"{descricao}: falhou após {tentativas} tentativas ({ultimo})")


def baixar_arquivo(
    url: str,
    destino: Path,
    *,
    sessao: requests.Session | None = None,
    tentativas: int = 4,
    timeout: tuple[int, int] = (15, 180),
    espera: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Baixa para um .part e só renomeia no fim, para não deixar arquivo pela metade."""
    sessao = sessao or nova_sessao()
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(destino.suffix + ".part")

    def chamada() -> requests.Response:
        return sessao.get(url, stream=True, timeout=timeout)

    r = _com_tentativas(chamada, tentativas=tentativas, espera=espera, descricao=f"download {url}")
    h = hashlib.sha256()
    total = 0
    with open(parcial, "wb") as f:
        for bloco in r.iter_content(chunk_size=1024 * 1024):
            if bloco:
                f.write(bloco)
                h.update(bloco)
                total += len(bloco)
    if total == 0:
        parcial.unlink(missing_ok=True)
        raise ErroFonte(f"download {url}: resposta vazia")
    parcial.replace(destino)
    return {
        "caminho": str(destino),
        "bytes": total,
        "sha256": h.hexdigest(),
        "last_modified": r.headers.get("Last-Modified"),
        "etag": r.headers.get("ETag"),
        "url_final": r.url,
    }


def extrair_zip_seguro(zip_path: Path, destino: Path) -> list[Path]:
    """Extrai o ZIP recusando caminhos fora do destino e links simbólicos."""
    destino = destino.resolve()
    destino.mkdir(parents=True, exist_ok=True)
    extraidos: list[Path] = []
    with zipfile.ZipFile(zip_path) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > LIMITE_ZIP_BYTES:
            raise ErroFonte(f"ZIP descompactado excede {LIMITE_ZIP_BYTES // 1024**3} GiB")
        for info in z.infolist():
            nome = PurePosixPath(info.filename.replace("\\", "/"))
            if nome.is_absolute() or ".." in nome.parts:
                raise ErroFonte(f"ZIP com caminho inseguro: {info.filename!r}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ErroFonte(f"ZIP com link simbólico: {info.filename!r}")
            alvo = (destino / nome).resolve()
            if destino not in alvo.parents and alvo != destino:
                raise ErroFonte(f"ZIP com caminho inseguro: {info.filename!r}")
            if info.is_dir():
                alvo.mkdir(parents=True, exist_ok=True)
                continue
            alvo.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(alvo, "wb") as dst:
                dst.write(src.read())
            extraidos.append(alvo)
    return extraidos


def localizar_vetor(diretorio: Path, padrao: str = "*.shp") -> Path:
    achados = sorted(p for p in diretorio.rglob(padrao) if p.is_file())
    if not achados:
        raise ErroFonte(f"nenhum arquivo {padrao!r} encontrado em {diretorio}")
    if len(achados) > 1:
        nomes = ", ".join(p.name for p in achados)
        raise ErroFonte(f"mais de um arquivo {padrao!r} em {diretorio} ({nomes}); ajuste 'arquivo_vetor'")
    return achados[0]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def descobrir_wfs(
    wfs_url: str,
    *,
    sessao: requests.Session | None = None,
    versao: str = "1.0.0",
    tentativas: int = 3,
    espera: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    """Lista typeName e título das camadas de um WFS (GetCapabilities)."""
    sessao = sessao or nova_sessao()
    params = {"service": "WFS", "version": versao, "request": "GetCapabilities"}
    r = _com_tentativas(
        lambda: sessao.get(wfs_url, params=params, timeout=(15, 120)),
        tentativas=tentativas, espera=espera, descricao=f"GetCapabilities {wfs_url}",
    )
    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError as exc:
        raise ErroFonte(f"GetCapabilities não retornou XML válido: {exc}") from exc
    camadas = []
    for ft in raiz.iter():
        if _local(ft.tag) != "FeatureType":
            continue
        nome = titulo = ""
        for filho in ft:
            if _local(filho.tag) == "Name":
                nome = (filho.text or "").strip()
            elif _local(filho.tag) == "Title":
                titulo = (filho.text or "").strip()
        if nome:
            camadas.append({"type_name": nome, "titulo": titulo})
    if not camadas:
        raise ErroFonte("GetCapabilities sem FeatureType (resposta inesperada)")
    return camadas


def baixar_wfs(
    wfs_url: str,
    type_name: str,
    destino: Path,
    *,
    versao: str = "1.0.0",
    srs: str = "EPSG:4326",
    pagina: int = 5000,
    sessao: requests.Session | None = None,
    tentativas: int = 4,
    espera: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Baixa uma camada WFS como GeoJSON. Na 2.0 pagina; na 1.x vem tudo de uma vez."""
    sessao = sessao or nova_sessao()
    destino.parent.mkdir(parents=True, exist_ok=True)
    v2 = not versao.startswith("1.")
    base = {
        "service": "WFS", "version": versao, "request": "GetFeature",
        "outputFormat": "application/json", "srsName": srs,
        ("typeNames" if v2 else "typeName"): type_name,
    }

    feicoes: list[dict] = []
    esperado: int | None = None
    inicio = 0
    while True:
        params = dict(base)
        if v2:
            params.update({"count": pagina, "startIndex": inicio})
        r = _com_tentativas(
            lambda p=params: sessao.get(wfs_url, params=p, timeout=(15, 300)),
            tentativas=tentativas, espera=espera, descricao=f"WFS {type_name} (início {inicio})",
        )
        try:
            corpo = json.loads(r.content)
        except ValueError as exc:
            trecho = r.text[:300].replace("\n", " ")
            raise ErroFonte(f"WFS {type_name}: resposta não é JSON ({trecho})") from exc
        if corpo.get("type") != "FeatureCollection":
            raise ErroFonte(f"WFS {type_name}: resposta não é FeatureCollection")
        lote = corpo.get("features", [])
        feicoes.extend(lote)
        for chave in ("numberMatched", "totalFeatures"):
            if isinstance(corpo.get(chave), int):
                esperado = corpo[chave]
        if not v2 or len(lote) < pagina:
            break
        inicio += pagina

    if not feicoes:
        raise ErroFonte(f"WFS {type_name}: nenhuma feição retornada")
    if esperado is not None and len(feicoes) < esperado:
        raise ErroFonte(f"WFS {type_name}: download incompleto ({len(feicoes)} de {esperado})")

    conteudo = json.dumps({"type": "FeatureCollection", "features": feicoes}, ensure_ascii=False).encode("utf-8")
    parcial = destino.with_suffix(destino.suffix + ".part")
    parcial.write_bytes(conteudo)
    parcial.replace(destino)
    return {
        "caminho": str(destino),
        "bytes": len(conteudo),
        "sha256": hashlib.sha256(conteudo).hexdigest(),
        "last_modified": None,
        "etag": None,
        "url_final": wfs_url,
        "feicoes": len(feicoes),
        "srs_transporte": srs,
    }

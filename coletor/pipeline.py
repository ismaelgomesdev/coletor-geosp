"""Roda uma camada de ponta a ponta: obter, tratar, recortar, conferir, carregar e publicar."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd

from . import catalogo, fetch, geo, postgis
from .config import Config
from .estilos import gerar_sld
from .geoserver import GeoServerClient

log = logging.getLogger(__name__)


class ErroPipeline(RuntimeError):
    pass


@dataclass
class Resultado:
    camada: str
    status: str = "falha"          # ok | falha
    inicio: str = ""
    fim: str = ""
    etapa_falha: str | None = None
    erro: str | None = None
    estatisticas: dict[str, Any] = field(default_factory=dict)
    servicos: dict[str, Any] = field(default_factory=dict)


def _pastas(dir_dados: Path, id_: str) -> dict[str, Path]:
    p = {"bruto": dir_dados / "bruto" / id_, "processado": dir_dados / "processado"}
    for d in p.values():
        d.mkdir(parents=True, exist_ok=True)
    return p


def obter(camada: dict, dir_bruto: Path, cfg: Config, sessao=None) -> tuple[Path, dict]:
    acesso = camada["tipo_acesso"]
    if acesso == "arquivo_local":
        origem = Path(camada["caminho"])
        if not origem.is_file():
            raise fetch.ErroFonte(f"arquivo_local não encontrado: {origem}")
        h = hashlib.sha256(origem.read_bytes()).hexdigest()
        return origem, {"sha256": h, "last_modified": None, "bytes": origem.stat().st_size}
    if acesso == "wfs":
        destino = dir_bruto / f"{camada['id']}.geojson"
        info = fetch.baixar_wfs(
            camada["wfs_url"], camada["type_name"], destino,
            versao=str(camada.get("wfs_versao", "1.0.0")),
            srs=f"EPSG:{cfg.projeto.get('wfs_srs_transporte', 4326)}", sessao=sessao)
        return destino, info
    # download_zip | download_arquivo
    nome = Path(camada["url"].split("?")[0]).name or f"{camada['id']}.bin"
    baixado = dir_bruto / nome
    info = fetch.baixar_arquivo(camada["url"], baixado, sessao=sessao)
    if acesso == "download_zip":
        extr = dir_bruto / "extraido"
        if extr.exists():
            shutil.rmtree(extr)
        fetch.extrair_zip_seguro(baixado, extr)
        return fetch.localizar_vetor(extr, camada.get("arquivo_vetor", "*.shp")), info
    return baixado, info


def ler(caminho: Path, camada: dict) -> gpd.GeoDataFrame:
    kw = {"engine": "pyogrio"}
    if camada.get("encoding"):
        kw["encoding"] = camada["encoding"]
    return gpd.read_file(caminho, **kw)


def aplicar_filtro(gdf: gpd.GeoDataFrame, filtro: dict | None) -> tuple[gpd.GeoDataFrame, str | None]:
    if not filtro:
        return gdf, None
    alvo = filtro["atributo"].lower()
    col = next((c for c in gdf.columns if c.lower() == alvo), None)
    if col is None:
        raise geo.ErroGeo(f"atributo de filtro {filtro['atributo']!r} não existe no arquivo "
                          f"(colunas: {', '.join(map(str, gdf.columns[:15]))}...)")
    mascara = gdf[col].astype(str).str.strip() == str(filtro["valor"])
    if not mascara.any():
        raise geo.ErroGeo(f"filtro {filtro['atributo']}={filtro['valor']} não encontrou nenhuma feição")
    return gdf[mascara].copy(), f"Filtro por atributo {col} = {filtro['valor']}"


def carregar_limite(cfg: Config, dir_processado: Path) -> gpd.GeoDataFrame:
    lim = cfg.limite()
    if not lim:
        raise ErroPipeline("nenhuma camada com papel: limite em fontes.yaml")
    arq = dir_processado / f"{lim['id']}.gpkg"
    if not arq.is_file():
        raise ErroPipeline(f"limite municipal ausente ({arq}); execute primeiro: python -m coletor executar {lim['id']}")
    return gpd.read_file(arq, engine="pyogrio")


def executar(
    cfg: Config, id_camada: str, *, env: dict[str, str], dir_dados: Path,
    engine=None, gs: GeoServerClient | None = None, sessao=None,
) -> Resultado:
    camada = cfg.camada(id_camada)
    inicio = datetime.now(timezone.utc)
    res = Resultado(camada=id_camada, inicio=inicio.isoformat(timespec="seconds"))
    etapa = "inicio"
    fonte_info: dict[str, Any] = {}
    regras: list[str] = []
    crs_final = int(cfg.projeto["crs_final"])
    schema = cfg.projeto["schema_pg"]
    tol = float(cfg.projeto.get("tolerancia_borda_m", 100))
    pastas = _pastas(dir_dados, id_camada)
    try:
        etapa = "obter"
        arquivo, fonte_info = obter(camada, pastas["bruto"], cfg, sessao)

        etapa = "ler"
        gdf = ler(arquivo, camada)
        lidos = len(gdf)
        res.estatisticas["registros_lidos"] = lidos

        etapa = "filtro"
        gdf, regra = aplicar_filtro(gdf, camada.get("filtro_atributo"))
        if regra:
            regras.append(regra)

        etapa = "padronizar"
        gdf, mapa = geo.padronizar_colunas(gdf)
        regras.append("Nomes de atributos em snake_case sem acento; geometria na coluna 'geom'")

        etapa = "crs"
        esperado = int(cfg.projeto.get("wfs_srs_transporte", 4326)) if camada["tipo_acesso"] == "wfs" \
            else camada.get("crs_original")
        gdf, info_crs = geo.padronizar_crs(gdf, esperado, crs_final)
        res.estatisticas["crs_arquivo"] = info_crs["crs_arquivo"]
        regras.append(f"Reprojeção de {info_crs['crs_arquivo']} para EPSG:{crs_final}")

        etapa = "geometrias"
        gdf, rep = geo.reparar_geometrias(gdf, camada["geometria"])
        res.estatisticas["reparo"] = rep
        regras.append("Geometrias nulas e vazias removidas, inválidas reparadas com make_valid, convertidas para 2D e Multi*")

        etapa = "recorte"
        e_limite = camada.get("papel") == "limite"
        if e_limite:
            u = geo.uniao_limite(gdf)
            res.estatisticas["recorte"] = {"observacao": "camada é o próprio limite"}
        else:
            lim_gdf = carregar_limite(cfg, pastas["processado"])
            u = geo.uniao_limite(lim_gdf)
            modo = "recortar" if camada["cobertura_fonte"] == "ampla" else "filtrar"
            gdf, rec = geo.recortar_ou_filtrar(gdf, u, camada["geometria"], modo=modo, tolerancia_m=tol)
            res.estatisticas["recorte"] = {"modo": modo, **rec}
            regras.append(
                "Recorte pelo limite municipal: o que está fora é descartado e linhas e polígonos são cortados"
                if modo == "recortar" else
                f"Filtro espacial pelo limite municipal (tolerância de {tol:g} m), sem alterar geometrias")

        etapa = "duplicidades"
        gdf, n_dup = geo.remover_duplicidades(gdf, camada.get("chave_unica"))
        res.estatisticas["duplicidades_removidas"] = n_dup
        regras.append("Duplicidades removidas" + (f" pela chave {camada['chave_unica']}" if camada.get("chave_unica") else " (geometria + atributos idênticos)"))

        etapa = "qc"
        qc = geo.verificar_limite(gdf, u, tol)
        res.estatisticas["qc_fora_do_limite"] = qc
        if not e_limite and qc["fora"] > 0:
            raise ErroPipeline(f"QC: {qc['fora']} feições fora do limite municipal (máx. {qc['max_distancia_m']} m)")
        n = len(gdf)
        if n < int(camada.get("min_registros", 1)):
            raise ErroPipeline(f"QC: {n} registros, abaixo do mínimo esperado ({camada.get('min_registros', 1)})")
        if camada.get("max_registros") and n > int(camada["max_registros"]):
            raise ErroPipeline(f"QC: {n} registros, acima do máximo esperado ({camada['max_registros']})")
        if camada.get("area_km2_esperada"):
            a = geo.area_km2(gdf)
            lo, hi = camada["area_km2_esperada"]
            res.estatisticas["area_km2"] = round(a, 2)
            if not lo <= a <= hi:
                raise ErroPipeline(f"QC: área {a:.1f} km² fora do intervalo esperado [{lo}, {hi}]")
        res.estatisticas["registros_final"] = n

        etapa = "salvar_local"
        saida = pastas["processado"] / f"{id_camada}.gpkg"
        tmp = saida.with_suffix(".novo.gpkg")
        tmp.unlink(missing_ok=True)
        gdf.to_file(tmp, driver="GPKG", layer=id_camada, engine="pyogrio")
        tmp.replace(saida)

        if engine is not None:
            etapa = "carga_postgis"
            postgis.carregar_camada(engine, gdf, schema, id_camada, srid=crs_final)

        etapa = "catalogo"
        registro = catalogo.montar_registro(cfg, camada, fonte_info, {
            "crs_arquivo": info_crs["crs_arquivo"], "registros_final": n,
            "dicionario": geo.dicionario_atributos(gdf), "regras": regras,
            "qualidade": dict(res.estatisticas),
        })
        catalogo.salvar_registro(Path(env["dir_catalogo"]), registro)

        if gs is not None and engine is not None:
            etapa = "geoserver"
            ws, ds = cfg.projeto["workspace_geoserver"], cfg.projeto["datastore_geoserver"]
            gs.garantir_workspace(ws)
            gs.garantir_datastore(ws, ds, host=env["gs_pg_host"], porta=env["gs_pg_port"], banco=env["pg_db"],
                                  usuario=env["gs_pg_user"], senha=env["gs_pg_password"], schema=schema)
            estilo = f"{id_camada}_estilo"
            gs.garantir_estilo(ws, estilo, gerar_sld(estilo, camada["titulo"], camada["geometria"], camada.get("estilo")))
            gs.publicar_camada(ws, ds, nome=id_camada, tabela=id_camada, titulo=camada["titulo"],
                               descricao=camada.get("descricao") or camada["titulo"], srs=crs_final, estilo=estilo,
                               palavras_chave=["São Paulo", camada["fonte"], "dados abertos"])
            etapa = "verificar_servicos"
            minx, miny, maxx, maxy = gdf.total_bounds
            res.servicos = gs.verificar_servicos(ws, id_camada, (minx, miny, maxx, maxy), crs_final)
            if not (res.servicos.get("wms") and res.servicos.get("wfs")):
                raise ErroPipeline(f"verificação de serviços falhou: {res.servicos}")

        res.status = "ok"
    except Exception as exc:
        res.etapa_falha = etapa
        res.erro = f"{type(exc).__name__}: {exc}"
        log.error("camada %s falhou na etapa %s: %s", id_camada, etapa, res.erro)
    finally:
        res.fim = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _registrar(res, env, engine, inicio, fonte_info)
    return res


def _registrar(res: Resultado, env: dict[str, str], engine, inicio: datetime, fonte_info: dict) -> None:
    """Grava em operacoes.jsonl e, se tiver banco, em meta.coletas."""
    try:
        logs = Path(env["dir_logs"])
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "operacoes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(res.__dict__, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.warning("não foi possível gravar operacoes.jsonl: %s", exc)
    if engine is not None:
        try:
            e = res.estatisticas
            postgis.registrar_coleta(
                engine, camada=res.camada, inicio=inicio, status=res.status,
                lidos=e.get("registros_lidos"), final=e.get("registros_final"),
                fora=(e.get("recorte") or {}).get("removidas_fora"),
                sha256=fonte_info.get("sha256"),
                mensagem=None if res.status == "ok" else f"[{res.etapa_falha}] {res.erro}")
        except Exception as exc:
            log.warning("não foi possível registrar em meta.coletas: %s", exc)

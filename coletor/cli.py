"""Linha de comando (python -m coletor <comando>)."""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import catalogo, fetch, geo, pipeline, postgis
from .config import ErroConfig, ambiente, carregar, carregar_env
from .geoserver import ErroGeoServer, GeoServerClient


def _logging(dir_logs: str, verboso: bool) -> None:
    Path(dir_logs).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.DEBUG if verboso else logging.INFO)
    if raiz.handlers:
        return
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    arquivo = RotatingFileHandler(Path(dir_logs) / "coletor.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    arquivo.setFormatter(fmt)
    raiz.addHandler(console)
    raiz.addHandler(arquivo)


def _gs(env) -> GeoServerClient:
    return GeoServerClient(env["gs_url"], env["gs_user"], env["gs_password"])


def cmd_fontes(cfg, args, env) -> int:
    print(f"{'id':22} {'ativa':6} {'fonte':9} {'acesso':14} {'verificação':20} título")
    for c in cfg.camadas:
        print(f"{c['id']:22} {'sim' if c.get('ativa') else 'não':6} {c['fonte']:9} {c['tipo_acesso']:14} "
              f"{c.get('verificacao', '-'):20} {c.get('titulo', '')}")
    return 0


def cmd_descobrir(cfg, args, env) -> int:
    url = cfg.fontes["geosampa"]["wfs"]
    itens = fetch.descobrir_wfs(url)
    filtro = (args.filtro or "").lower()
    achados = [i for i in itens if filtro in i["type_name"].lower() or filtro in i["titulo"].lower()]
    for i in achados:
        print(f"{i['type_name']:60} {i['titulo']}")
    print(f"\n{len(achados)} de {len(itens)} camadas", file=sys.stderr)
    return 0


def cmd_inspecionar(cfg, args, env) -> int:
    import geopandas as gpd
    gdf = gpd.read_file(args.arquivo, engine="pyogrio")
    print(f"registros: {len(gdf)}\nCRS: {gdf.crs}\ntipos: {sorted(set(gdf.geom_type.dropna()))}")
    print("colunas:", ", ".join(map(str, gdf.columns)))
    print(gdf.drop(columns=gdf.geometry.name).head(5).to_string())
    return 0


def cmd_ambiente(cfg, args, env) -> int:
    ok = True
    try:
        engine = postgis.criar_engine(env)
        if args.preparar:
            postgis.preparar_banco(engine, cfg.projeto["schema_pg"])
            if env["gs_pg_user"] != env["pg_user"]:
                if postgis.conceder_leitura(engine, cfg.projeto["schema_pg"], env["gs_pg_user"]):
                    print(f"SELECT concedido a {env['gs_pg_user']}")
                else:
                    print(f"aviso: o usuário {env['gs_pg_user']} não existe no banco, crie antes de publicar")
        print("PostgreSQL/PostGIS:", postgis.validar_ambiente(engine))
    except Exception as exc:
        ok = False
        print(f"PostgreSQL/PostGIS: FALHA, {exc}")
    try:
        gs = _gs(env)
        print("GeoServer:", gs.versao(), "em", env["gs_url"])
        if args.preparar:
            gs.garantir_workspace(cfg.projeto["workspace_geoserver"])
            gs.garantir_datastore(cfg.projeto["workspace_geoserver"], cfg.projeto["datastore_geoserver"],
                                  host=env["gs_pg_host"], porta=env["gs_pg_port"], banco=env["pg_db"],
                                  usuario=env["gs_pg_user"], senha=env["gs_pg_password"], schema=cfg.projeto["schema_pg"])
            print("workspace e datastore garantidos")
    except ErroGeoServer as exc:
        ok = False
        print(f"GeoServer: FALHA, {exc}")
    return 0 if ok else 1


def cmd_executar(cfg, args, env) -> int:
    ids = [c["id"] for c in cfg.ativas()] if args.todas else args.camadas
    if not ids:
        print("informe camadas ou use --todas", file=sys.stderr)
        return 2
    # as outras camadas dependem do limite, então ele roda primeiro
    lim = cfg.limite()
    if lim and lim["id"] in ids:
        ids = [lim["id"]] + [i for i in ids if i != lim["id"]]
    engine = None if args.sem_banco else postgis.criar_engine(env)
    if engine is not None:
        postgis.preparar_banco(engine, cfg.projeto["schema_pg"])
    gs = None if (args.sem_banco or args.sem_publicar) else _gs(env)
    falhas = 0
    for id_ in ids:
        r = pipeline.executar(cfg, id_, env=env, dir_dados=Path(env["dir_dados"]), engine=engine, gs=gs)
        marca = "OK   " if r.status == "ok" else "FALHA"
        print(f"[{marca}] {id_}: {r.estatisticas.get('registros_final', '-')} registros"
              + (f" | etapa {r.etapa_falha}: {r.erro}" if r.erro else "")
              + (f" | serviços {r.servicos}" if r.servicos else ""))
        falhas += r.status != "ok"
    return 1 if falhas else 0


def cmd_verificar(cfg, args, env) -> int:
    import geopandas as gpd
    gs = _gs(env)
    ws = cfg.projeto["workspace_geoserver"]
    arq = Path(env["dir_dados"]) / "processado" / f"{args.camada}.gpkg"
    bbox = tuple(gpd.read_file(arq, engine="pyogrio").total_bounds)
    r = gs.verificar_servicos(ws, args.camada, bbox, int(cfg.projeto["crs_final"]))
    print(r)
    return 0 if r.get("wms") and r.get("wfs") else 1


def cmd_catalogo(cfg, args, env) -> int:
    regs = catalogo.carregar_registros(Path(env["dir_catalogo"]))
    if not regs:
        print("catálogo vazio: execute ao menos uma camada", file=sys.stderr)
        return 1
    d = Path(env["dir_catalogo"])
    print(catalogo.exportar_csv(regs, d / "catalogo_metadados.csv"))
    print(catalogo.exportar_markdown(regs, d / "catalogo_metadados.md"))
    return 0


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coletor", description="Coletor de dados geoespaciais do município de São Paulo")
    p.add_argument("--config", help="caminho do fontes.yaml (padrão: config/fontes.yaml)")
    p.add_argument("-v", "--verboso", action="store_true")
    sp = p.add_subparsers(dest="comando", required=True)
    sp.add_parser("fontes", help="lista as camadas do fontes.yaml").set_defaults(f=cmd_fontes)
    d = sp.add_parser("descobrir", help="lista os typeNames do WFS do GeoSampa")
    d.add_argument("--filtro", help="texto para filtrar nome/título")
    d.set_defaults(f=cmd_descobrir)
    i = sp.add_parser("inspecionar", help="mostra CRS e colunas de um arquivo vetorial")
    i.add_argument("arquivo")
    i.set_defaults(f=cmd_inspecionar)
    a = sp.add_parser("ambiente", help="valida PostGIS e GeoServer")
    a.add_argument("--preparar", action="store_true", help="cria schemas, permissões, workspace e datastore")
    a.set_defaults(f=cmd_ambiente)
    e = sp.add_parser("executar", help="coleta, trata, carrega e publica")
    e.add_argument("camadas", nargs="*")
    e.add_argument("--todas", action="store_true", help="todas as camadas ativas")
    e.add_argument("--sem-banco", action="store_true", help="só baixa, trata e grava o GeoPackage")
    e.add_argument("--sem-publicar", action="store_true", help="carrega no PostGIS e não publica")
    e.set_defaults(f=cmd_executar)
    v = sp.add_parser("verificar", help="testa WMS e WFS de uma camada publicada")
    v.add_argument("camada")
    v.set_defaults(f=cmd_verificar)
    sp.add_parser("catalogo", help="exporta o catálogo em CSV e Markdown").set_defaults(f=cmd_catalogo)
    return p


def main(argv: list[str] | None = None) -> int:
    carregar_env()
    args = construir_parser().parse_args(argv)
    env = ambiente()
    _logging(env["dir_logs"], args.verboso)
    try:
        cfg = carregar(args.config)
        return args.f(cfg, args, env)
    except (ErroConfig, fetch.ErroFonte, geo.ErroGeo, postgis.ErroCarga, ErroGeoServer, pipeline.ErroPipeline) as exc:
        logging.getLogger("coletor").error("%s", exc)
        return 1

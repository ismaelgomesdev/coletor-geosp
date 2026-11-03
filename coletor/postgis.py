"""Carga no PostGIS. A tabela nova só entra no lugar da antiga depois de validada."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import geopandas as gpd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

log = logging.getLogger(__name__)


class ErroCarga(RuntimeError):
    pass


def criar_engine(env: dict[str, str]) -> Engine:
    url = URL.create(
        "postgresql+psycopg",
        username=env["pg_user"], password=env["pg_password"] or None,
        host=env["pg_host"], port=int(env["pg_port"]), database=env["pg_db"],
    )
    return create_engine(url, pool_pre_ping=True)


def _q(engine: Engine, nome: str) -> str:
    return engine.dialect.identifier_preparer.quote(nome)


def validar_ambiente(engine: Engine) -> dict[str, str]:
    with engine.connect() as con:
        versao = con.execute(text("SHOW server_version")).scalar()
        try:
            postgis = con.execute(text("SELECT postgis_version()")).scalar()
        except Exception as exc:
            raise ErroCarga("PostGIS não está instalado neste banco (CREATE EXTENSION postgis)") from exc
    return {"postgresql": str(versao), "postgis": str(postgis)}


def preparar_banco(engine: Engine, schema: str) -> None:
    """Cria os schemas e a tabela meta.coletas se ainda não existirem."""
    with engine.begin() as con:
        try:
            con.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        except Exception:
            log.warning("não foi possível criar a extensão postgis (precisa de superusuário); seguindo")
    with engine.begin() as con:
        con.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_q(engine, schema)}"))
        con.execute(text("CREATE SCHEMA IF NOT EXISTS meta"))
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS meta.coletas (
                id BIGSERIAL PRIMARY KEY, camada TEXT NOT NULL,
                inicio TIMESTAMPTZ NOT NULL, fim TIMESTAMPTZ, status TEXT NOT NULL,
                registros_lidos INTEGER, registros_final INTEGER, registros_fora INTEGER,
                sha256_fonte TEXT, mensagem TEXT)
        """))


def conceder_leitura(engine: Engine, schema: str, usuario: str) -> bool:
    """Dá SELECT ao usuário que o GeoServer usa. Devolve False se o usuário não existe."""
    s, u = _q(engine, schema), _q(engine, usuario)
    with engine.begin() as con:
        existe = con.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :u"), {"u": usuario}).scalar()
        if not existe:
            return False
        con.execute(text(f"GRANT USAGE ON SCHEMA {s} TO {u}"))
        con.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {u}"))
        con.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT ON TABLES TO {u}"))
    return True


def carregar_camada(
    engine: Engine, gdf: gpd.GeoDataFrame, schema: str, tabela: str, *, srid: int
) -> dict[str, Any]:
    """
    Carrega numa tabela temporária, confere contagem, geometrias e SRID e só então troca
    pelo nome definitivo, numa transação. Se algo falhar a tabela atual fica como estava.
    A versão que saiu fica em <tabela>__anterior.
    """
    estagio = f"{tabela}__n{uuid.uuid4().hex[:8]}"
    anterior = f"{tabela}__anterior"
    s, e, t, a = _q(engine, schema), _q(engine, estagio), _q(engine, tabela), _q(engine, anterior)
    if gdf.geometry.name != "geom":
        gdf = gdf.rename_geometry("geom")

    try:
        gdf.to_postgis(estagio, engine, schema=schema, if_exists="fail", index=False)
        with engine.begin() as con:
            con.execute(text(f"ALTER TABLE {s}.{e} ADD COLUMN gid BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY"))
            # o to_postgis já cria o índice GiST, isto é só por segurança
            tem_gist = con.execute(text(
                "SELECT count(*) FROM pg_indexes WHERE schemaname=:s AND tablename=:t AND indexdef ILIKE '%gist%'"),
                {"s": schema, "t": estagio}).scalar()
            if not tem_gist:
                con.execute(text(f"CREATE INDEX ON {s}.{e} USING GIST (geom)"))
            n = con.execute(text(f"SELECT count(*) FROM {s}.{e}")).scalar()
            invalidas = con.execute(text(f"SELECT count(*) FROM {s}.{e} WHERE NOT ST_IsValid(geom)")).scalar()
            srid_tab = con.execute(text(f"SELECT Find_SRID(:s, :t, 'geom')"), {"s": schema, "t": estagio}).scalar()
        if n != len(gdf):
            raise ErroCarga(f"contagem divergente após a carga: {n} no banco vs {len(gdf)} no arquivo")
        if invalidas:
            raise ErroCarga(f"{invalidas} geometrias inválidas no banco")
        if srid_tab != srid:
            raise ErroCarga(f"SRID {srid_tab} no banco; esperado {srid}")

        with engine.begin() as con:
            con.execute(text(f"DROP TABLE IF EXISTS {s}.{a}"))
            existe = con.execute(text("SELECT to_regclass(:n)"), {"n": f"{schema}.{tabela}"}).scalar()
            if existe:
                con.execute(text(f"ALTER TABLE {s}.{t} RENAME TO {a}"))
            con.execute(text(f"ALTER TABLE {s}.{e} RENAME TO {t}"))
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as con:
            con.execute(text(f"ANALYZE {s}.{t}"))
    except Exception as exc:
        with engine.begin() as con:
            con.execute(text(f"DROP TABLE IF EXISTS {s}.{e}"))
        if isinstance(exc, ErroCarga):
            raise
        raise ErroCarga(f"falha na carga de {schema}.{tabela}: {exc}") from exc

    return {"tabela": f"{schema}.{tabela}", "registros": int(n), "srid": srid}


def registrar_coleta(
    engine: Engine, *, camada: str, inicio: datetime, status: str,
    lidos: int | None = None, final: int | None = None, fora: int | None = None,
    sha256: str | None = None, mensagem: str | None = None,
) -> None:
    with engine.begin() as con:
        con.execute(text("""
            INSERT INTO meta.coletas
              (camada, inicio, fim, status, registros_lidos, registros_final, registros_fora, sha256_fonte, mensagem)
            VALUES (:camada, :inicio, now(), :status, :lidos, :final, :fora, :sha, :msg)
        """), {"camada": camada, "inicio": inicio, "status": status, "lidos": lidos,
               "final": final, "fora": fora, "sha": sha256, "msg": (mensagem or "")[:2000] or None})


def ler_camada(engine: Engine, schema: str, tabela: str) -> gpd.GeoDataFrame:
    return gpd.read_postgis(f"SELECT * FROM {_q(engine, schema)}.{_q(engine, tabela)}", engine, geom_col="geom")


def tabela_existe(engine: Engine, schema: str, tabela: str) -> bool:
    with engine.connect() as con:
        return bool(con.execute(text("SELECT to_regclass(:n)"), {"n": f"{schema}.{tabela}"}).scalar())

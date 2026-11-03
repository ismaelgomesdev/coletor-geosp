-- Roda uma vez, na criação do banco (docker-entrypoint-initdb.d).
-- Num PostgreSQL que já existe, rode à mão ou use: python -m coletor ambiente --preparar
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS sp;    -- camadas publicadas, só do município de São Paulo
CREATE SCHEMA IF NOT EXISTS meta;  -- registro das coletas

CREATE TABLE IF NOT EXISTS meta.coletas (
    id              BIGSERIAL PRIMARY KEY,
    camada          TEXT        NOT NULL,
    inicio          TIMESTAMPTZ NOT NULL,
    fim             TIMESTAMPTZ,
    status          TEXT        NOT NULL,          -- ok ou falha
    registros_lidos INTEGER,
    registros_final INTEGER,
    registros_fora  INTEGER,
    sha256_fonte    TEXT,
    mensagem        TEXT
);

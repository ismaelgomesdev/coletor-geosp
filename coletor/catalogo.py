"""Catálogo de metadados: um JSON por camada, mais CSV e Markdown consolidados."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CAMPOS_CSV = [
    "id", "titulo", "descricao", "orgao_produtor", "url_origem", "url_metadados", "url_download",
    "licenca", "atribuicao", "data_referencia", "periodicidade", "data_coleta", "data_atualizacao_orgao",
    "crs_original", "crs_final", "cobertura_territorial", "formato", "quantidade_registros", "sha256_fonte",
    "verificacao_fonte", "observacao",
]


def montar_registro(cfg, camada: dict[str, Any], fonte_info: dict[str, Any], resultado: dict[str, Any]) -> dict[str, Any]:
    fonte = cfg.fontes[camada["fonte"]]
    acesso = camada["tipo_acesso"]
    url_download = camada.get("url") or (
        f'{camada.get("wfs_url")}?typeName={camada.get("type_name")}' if acesso == "wfs" else None)
    return {
        "id": camada["id"],
        "titulo": camada.get("titulo"),
        "descricao": camada.get("descricao") or camada.get("titulo"),
        "orgao_produtor": camada.get("orgao_produtor") or fonte.get("orgao"),
        "url_origem": camada.get("url_origem") or fonte.get("portal"),
        "url_metadados": camada.get("url_metadados"),
        "url_download": url_download,
        "licenca": fonte.get("licenca"),
        "atribuicao": fonte.get("atribuicao"),
        "data_referencia": camada.get("data_referencia"),
        "periodicidade": camada.get("periodicidade"),
        "data_coleta": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_atualizacao_orgao": fonte_info.get("last_modified"),
        "crs_original": resultado.get("crs_arquivo") or (f"EPSG:{camada['crs_original']}" if camada.get("crs_original") else None),
        "crs_final": f"EPSG:{cfg.projeto['crs_final']}",
        "cobertura_territorial": f"Município de {cfg.projeto['municipio']} (IBGE {cfg.projeto['codigo_ibge']})",
        "formato": camada.get("formato"),
        "quantidade_registros": resultado.get("registros_final"),
        "sha256_fonte": fonte_info.get("sha256"),
        "verificacao_fonte": camada.get("verificacao"),
        "observacao": camada.get("observacao"),
        "dicionario_atributos": resultado.get("dicionario", []),
        "regras_transformacao": resultado.get("regras", []),
        "qualidade": resultado.get("qualidade", {}),
    }


def salvar_registro(dir_catalogo: Path, registro: dict[str, Any]) -> Path:
    dir_catalogo.mkdir(parents=True, exist_ok=True)
    destino = dir_catalogo / f"{registro['id']}.json"
    destino.write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    return destino


def carregar_registros(dir_catalogo: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(dir_catalogo.glob("*.json"))]


def exportar_csv(registros: list[dict[str, Any]], destino: Path) -> Path:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_CSV, extrasaction="ignore", delimiter=";")
        w.writeheader()
        for r in registros:
            w.writerow(r)
    return destino


def exportar_markdown(registros: list[dict[str, Any]], destino: Path) -> Path:
    linhas = ["# Catálogo de metadados das camadas", ""]
    for r in registros:
        linhas += [f"## {r['titulo']} (`{r['id']}`)", "",
                   f"- Descrição: {r.get('descricao')}",
                   f"- Órgão produtor: {r.get('orgao_produtor')}",
                   f"- Origem: {r.get('url_origem')}",
                   f"- Metadados: {r.get('url_metadados') or 'não informado'}",
                   f"- Download: {r.get('url_download')}",
                   f"- Licença: {r.get('licenca')}",
                   f"- Atribuição: {r.get('atribuicao')}",
                   f"- Data de referência: {r.get('data_referencia')}",
                   f"- Periodicidade: {r.get('periodicidade') or 'não informada'}",
                   f"- Data da coleta: {r.get('data_coleta')}",
                   f"- Última modificação no órgão: {r.get('data_atualizacao_orgao') or 'a fonte não informa'}",
                   f"- CRS: {r.get('crs_original')} (original), {r.get('crs_final')} (final)",
                   f"- Cobertura: {r.get('cobertura_territorial')}",
                   f"- Formato: {r.get('formato')}",
                   f"- Registros: {r.get('quantidade_registros')}",
                   f"- Situação da fonte: {r.get('verificacao_fonte')}", ""]
        if r.get("regras_transformacao"):
            linhas += ["Regras de transformação:", ""] + [f"{i}. {x}" for i, x in enumerate(r["regras_transformacao"], 1)] + [""]
        if r.get("dicionario_atributos"):
            linhas += ["Dicionário de atributos:", "", "| atributo | tipo | nulos | exemplo |", "|---|---|---|---|"]
            for a in r["dicionario_atributos"]:
                ex = str(a.get("exemplo") or "").replace("|", "/")
                linhas.append(f"| {a['nome']} | {a['tipo']} | {a['nulos']} | {ex} |")
            linhas.append("")
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("\n".join(linhas), encoding="utf-8")
    return destino

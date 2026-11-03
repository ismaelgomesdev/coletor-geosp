# coletor-geosp

Coletor em Python de dados geoespaciais abertos do município de São Paulo (IBGE 3550308). Baixa os dados,
trata, recorta pelo limite municipal, carrega no PostGIS e publica no GeoServer pela API REST.
Projeto de pesquisa voluntária no Insight Data Science Lab (UFC), outubro a dezembro de 2026.

Situação ao fim de outubro de 2026: as duas camadas de teste (limite municipal e UBS) foram coletadas de
verdade e publicadas num GeoServer 2.26.2 local, em Docker. Falta repetir no GeoServer do laboratório.

## Como funciona

Cada camada passa pelas mesmas etapas:

1. baixa a fonte (ZIP do IBGE, WFS do GeoSampa ou arquivo local), com retentativas e hash SHA-256
2. filtra por atributo quando precisa (ex.: `CD_MUN = 3550308`)
3. padroniza os nomes das colunas, reprojeta para EPSG:31983 e repara geometrias
4. recorta ou filtra pelo limite do município e remove duplicados
5. confere se alguma feição ficou fora do limite e se a contagem e a área fazem sentido
6. grava um GeoPackage em `dados/processado/` e carrega no PostGIS
7. gera o metadado em `catalogo/` e registra a operação em `logs/operacoes.jsonl` e na tabela `meta.coletas`
8. cadastra workspace, datastore, estilo e camada no GeoServer e testa WMS GetMap e WFS GetFeature

A carga no PostGIS usa uma tabela temporária. Ela só toma o lugar da tabela publicada depois de conferida
(contagem, geometrias válidas e SRID), e a versão anterior fica em `<camada>__anterior`. Se qualquer etapa
falhar, a camada que já estava publicada continua como estava.

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate        # no Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # preencher as senhas do PostgreSQL e do GeoServer
```

Para o banco, ou usa um PostgreSQL com PostGIS que já exista (rodando `db/init/01_postgis.sql`), ou sobe um
local com `docker compose up -d postgis`.

O GeoServer deveria ler o banco com um usuário só de leitura. Crie o usuário antes e coloque o nome dele em
`GEOSERVER_PG_USER`:

```sql
CREATE ROLE geoserver_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE geosp TO geoserver_ro;
```

O `ambiente --preparar` (abaixo) já dá as permissões de leitura no schema `sp`.

## Uso

```bash
python -m coletor ambiente --preparar        # confere PostGIS e GeoServer, cria schemas, permissões, workspace e datastore
python -m coletor fontes                     # lista as camadas do fontes.yaml
python -m coletor descobrir --filtro ubs     # typeNames do WFS do GeoSampa
python -m coletor executar limite_municipal  # uma camada (o limite sempre roda primeiro)
python -m coletor executar --todas           # todas as camadas ativas
python -m coletor executar --todas --sem-banco       # só baixa e trata, grava o GeoPackage
python -m coletor executar --todas --sem-publicar    # carrega no PostGIS e não publica
python -m coletor verificar ubs_unidades_saude       # testa WMS e WFS de uma camada publicada
python -m coletor inspecionar arquivo.shp    # CRS, colunas e amostra de um arquivo
python -m coletor catalogo                   # gera catalogo/catalogo_metadados.csv e .md
```

Pastas geradas: `dados/bruto/`, `dados/processado/`, `catalogo/`, `logs/`.
O código de saída é 1 se alguma camada falhar.

## Cadastrar uma fonte ou camada

Tudo fica em `config/fontes.yaml`. Copie um bloco de `camadas`, troque o `id` (letras minúsculas, números e `_`)
e preencha:

| campo | para que serve |
|---|---|
| `tipo_acesso` | `download_zip`, `download_arquivo`, `wfs` ou `arquivo_local` (`gtfs` ainda não existe) |
| `url`, ou `wfs_url` + `type_name` | de onde vêm os dados |
| `wfs_versao` | `1.0.0` baixa tudo de uma vez; `2.0.0` baixa paginado (melhor para camada grande) |
| `geometria` | `ponto`, `linha` ou `poligono` |
| `cobertura_fonte` | `ampla` (o arquivo tem outros municípios, então recorta) ou `municipal` (só filtra) |
| `filtro_atributo` | ex.: `{atributo: CD_MUN, valor: "3550308"}` |
| `chave_unica` | colunas (já em snake_case) que identificam um registro, para tirar duplicados |
| `min_registros`, `max_registros`, `area_km2_esperada` | conferência de qualidade |
| `data_referencia`, `periodicidade`, `url_metadados` | vão direto para o catálogo |
| `ativa: true` | só assim o `--todas` roda a camada |

O arquivo é validado ao carregar, então uma camada ativa sem URL ou typeName dá erro logo.

Para atualizar uma camada é só rodar de novo. O cadastro no GeoServer cria o que não existe e atualiza o que
existe, e a tabela é trocada inteira, então não duplica registro.

## Execução periódica

Exemplo de cron, todo domingo às 3h:

```
0 3 * * 0  cd /caminho/coletor-geosp && .venv/bin/python -m coletor executar --todas >> logs/cron.log 2>&1
```

No Windows dá para usar o Agendador de Tarefas com o mesmo comando.

## Testes

```bash
pytest -q
```

São 37 testes. Três precisam de um PostGIS de teste e são pulados se ele não estiver configurado:

```bash
TEST_PG_HOST=localhost TEST_PG_PORT=5432 TEST_PG_DB=geosp TEST_PG_USER=coletor TEST_PG_PASSWORD=... pytest -q
```

Esses testes criam e apagam o schema `sp_teste` e esvaziam `meta.coletas`, então não rode contra o banco de
verdade. A API do GeoServer é testada contra um servidor HTTP falso; o teste com o GeoServer real foi feito à
mão (ver o relatório).

## O que ainda falta

- Conector do GTFS da SPTrans (previsto para novembro).
- Conferir typeNames e atributos das camadas que estão com `ativa: false`.
- Os estilos são SLD simples, um por tipo de geometria, sem rótulos.
- A tolerância de 100 m na borda e o uso do limite do IBGE como máscara ainda precisam do aval do orientador.

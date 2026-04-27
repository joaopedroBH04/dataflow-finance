# DataFlow Finance — ETL Dedicado para Restaurantes

> **Infraestrutura de engenharia de dados sob medida para restaurantes de médio e grande porte.**
> Elimine furos de caixa, consolide múltiplos canais de venda e automatize seu DRE.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)]()
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688)]()
[![Pandas](https://img.shields.io/badge/Pandas-2.2-150458)]()
[![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063)]()
[![License](https://img.shields.io/badge/License-Proprietary-lightgrey)]()
[![Changelog](https://img.shields.io/badge/Changelog-CHANGELOG.md-informational)](CHANGELOG.md)

---

## Visão Geral

O **DataFlow Finance** é um microsserviço ETL de alta performance que integra dados de:
- **PDV local** (qualquer vendor com export CSV/XLSX)
- **Plataformas de delivery** (iFood, Rappi, Uber Eats)
- **Adquirentes** (Stone, Cielo, Getnet, Rede)

E entrega:
- DRE consolidado auditável
- Detecção automática de furos de caixa
- Alertas via webhook (Telegram, Slack, WhatsApp Business)
- Relatório Excel com 4 abas para a equipe financeira

---

## Arquitetura

```
┌─────────────────┐      ┌────────────────────┐      ┌──────────────────┐
│  Fontes         │      │  Motor ETL         │      │  Saídas          │
│                 │      │                    │      │                  │
│  • iFood        │─────▶│  1. Extract        │─────▶│  • DRE JSON      │
│  • PDV          │      │  2. Validate       │      │  • Excel (4 abas)│
│  • Stone/Cielo  │      │  3. Transform      │      │  • Webhooks      │
│                 │      │  4. Detect Gaps    │      │  • Dashboard API │
└─────────────────┘      │  5. Load           │      └──────────────────┘
                         │                    │
                         │  Logs + Quarantine │
                         └────────────────────┘
```

### Stack Técnica

| Camada          | Tecnologia                        |
|-----------------|-----------------------------------|
| API             | FastAPI + Uvicorn                 |
| Validação       | Pydantic v2                       |
| Processamento   | Pandas 2.x + NumPy                |
| Observabilidade | Loguru (structured JSON logs)     |
| Config          | pydantic-settings + .env          |
| Output          | OpenPyXL (Excel), JSON            |

---

## Setup Local

### 1. Clonar e instalar dependências

```bash
git clone https://github.com/joaopedroBH04/dataflow-finance.git
cd dataflow-finance
python -m pip install -r requirements.txt
# For development / testing:
python -m pip install -r requirements-dev.txt
```

### 2. Configurar variáveis de ambiente

```bash
cp .env.example .env
# Edite .env conforme seu contrato
```

### 3. Rodar o servidor

```bash
python -m uvicorn etl_service.main:app --reload --port 8000
```

Acesse:
- **Swagger UI**: http://localhost:8000/docs
- **Health check**: http://localhost:8000/health
- **Landing page**: abra `index.html` no navegador

---

## Uso da API

A documentação interativa completa está disponível em **http://localhost:8000/docs** (Swagger UI).

### Executar pipeline ETL

```bash
curl -X POST http://localhost:8000/api/v1/run-etl \
  -H "Content-Type: application/json" \
  -d '{
    "ifood_file_path": "./data/ifood_2026-03.csv",
    "pdv_file_path": "./data/pdv_2026-03.csv",
    "acquirer_file_path": "./data/stone_2026-03.csv",
    "reference_month": "2026-03",
    "acquirer_name": "stone"
  }'
```

### Resposta esperada

```json
{
  "status": "success",
  "reference_month": "2026-03",
  "total_ifood_orders": 1284,
  "total_pdv_transactions": 3417,
  "total_acquirer_transactions": 2891,
  "quarantined_rows": 12,
  "gross_revenue_brl": 487320.50,
  "total_fees_brl": 42891.18,
  "net_revenue_brl": 444429.32,
  "gaps_detected": 7,
  "output_file_path": "./output/dataflow_2026-03_20260414_233751.xlsx"
}
```

---

### Dashboard de Métricas

```bash
# KPIs consolidados dos últimos 6 meses
curl http://localhost:8000/api/v1/metrics/dashboard

# Métricas de um mês específico
curl http://localhost:8000/api/v1/metrics/period/2026-03
```

---

### Captura de Leads

```bash
# Registrar lead (chamado pelo formulário da landing page)
curl -X POST http://localhost:8000/api/v1/leads/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Carlos Silva",
    "restaurant": "Burger Prime",
    "revenue": "R$ 100k - R$ 300k/mês",
    "systems": ["iFood", "Stone"],
    "phone": "31999990000"
  }'

# Listar leads — requer X-API-Key se DATAFLOW_LEADS_API_KEY estiver configurado
curl http://localhost:8000/api/v1/leads/?limit=50 \
  -H "X-API-Key: $DATAFLOW_LEADS_API_KEY"
```

---

### Sistema de Alertas / Webhooks

```bash
# Registrar webhook para receber alertas de furos
curl -X POST http://localhost:8000/api/v1/alerts/subscribers \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "slack",
    "webhook_url": "https://hooks.slack.com/services/...",
    "severity_filter": "medium"
  }'

# Listar alertas abertos (não reconhecidos)
curl http://localhost:8000/api/v1/alerts/active

# Reconhecer um alerta
curl -X POST http://localhost:8000/api/v1/alerts/{alert_id}/ack
```

---

## Estrutura do Projeto

```
dataflow-finance/
├── etl_service/
│   ├── config.py                   # Settings (pydantic-settings)
│   ├── main.py                     # FastAPI app entrypoint
│   ├── extractors/
│   │   ├── base.py                 # BaseExtractor (ABC + Factory)
│   │   ├── ifood.py                # iFood CSV extractor
│   │   ├── pdv.py                  # PDV CSV/XLSX extractor
│   │   └── stone_cielo.py          # Stone/Cielo acquirer extractor
│   ├── validators/
│   │   └── schemas.py              # Pydantic schemas + ETLRequest/Response
│   ├── transformers/
│   │   └── financial.py            # Merge, business rules, gap detection
│   └── loaders/
│       └── report.py               # Excel + JSON report writer
├── index.html                      # Landing page (B2B sales)
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Business Rules Implementadas

| Regra                           | Padrão   | Configurável via env              |
|---------------------------------|----------|-----------------------------------|
| Comissão iFood                  | 23%      | `DATAFLOW_IFOOD_COMMISSION_RATE`  |
| MDR Cartão de Crédito           | 2.99%    | `DATAFLOW_CREDIT_CARD_FEE`        |
| MDR Cartão de Débito            | 1.49%    | `DATAFLOW_DEBIT_CARD_FEE`         |
| Taxa PIX                        | 0.99%    | `DATAFLOW_PIX_FEE`                |
| Taxa Dinheiro                   | 0%       | `DATAFLOW_CASH_FEE`               |
| Tolerância para Gap Detection   | R$ 0.05  | `DATAFLOW_GAP_TOLERANCE_BRL`      |
| API Key para leitura de leads   | (vazio)  | `DATAFLOW_LEADS_API_KEY`          |

---

## Tipos de Furo de Caixa Detectados

1. **`PDV_MISSING_IN_ACQUIRER`** — Transação de cartão registrada no PDV mas sem liquidação na Stone/Cielo.
2. **`ACQUIRER_MISSING_IN_PDV`** — Liquidação no adquirente sem transação correspondente no PDV.
3. **`AMOUNT_MISMATCH`** — Valores divergem além da tolerância configurada.
4. **`IFOOD_ORDER_CANCELLED`** — Pedido iFood cancelado que pode estar sendo contabilizado como receita.

---

## Desenvolvimento

### Instalar dependências de desenvolvimento

```bash
pip install -r requirements-dev.txt
```

### Executar testes

```bash
make test
# ou: pytest tests/ -v --tb=short
```

### Lint e formatação

```bash
make lint
# ou: ruff check . --fix && ruff format .
```

### Verificação de tipos

```bash
make type-check
# ou: mypy etl_service/ --ignore-missing-imports
```

> Consulte o `Makefile` para a lista completa de atalhos disponíveis.

---

## Roadmap

- [x] Pipeline ETL base (Extract → Validate → Transform → Load)
- [x] Detecção de furos de caixa (4 tipos)
- [x] API REST com FastAPI
- [x] Landing page B2B
- [x] Sistema de alertas via webhook (Telegram, Slack, WhatsApp)
- [x] Dashboard de métricas KPI (`GET /metrics/dashboard`)
- [x] Captura de leads com autenticação por API key
- [ ] Dashboard web (React + Recharts)
- [ ] Integração Omie / Conta Azul
- [ ] Autenticação multi-tenant completa (múltiplos clientes isolados)
- [ ] Pipeline schedulado (cron / Celery Beat)

---

## Licença

Proprietário — Todos os direitos reservados. Contato para licenciamento comercial.
